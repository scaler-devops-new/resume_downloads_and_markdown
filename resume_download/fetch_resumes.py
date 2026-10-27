#!/usr/bin/env python3
"""Download authorized Scaler resumes listed in a CSV/TSV file.

Flow per learner:
  1. GET https://www.scaler.com/admin/fetch-resume/?... with the admin session
     cookie. This returns either the PDF itself or a small HTML page that polls
     a moonshot.scaler.com precomputed-asset URL.
  2. Poll that asset URL (without cookies) until it redirects to a signed S3
     URL that serves the PDF.

Inputs:
  1. A text file containing the complete browser "Copy as cURL" command.
  2. A CSV/TSV file with columns: Email, Resume URL (default)

Security properties:
  - The session cookie and browser headers are sent only to www.scaler.com.
    Every other host receives only a small header allowlist.
  - Asset URLs, signed S3 URLs and cookies are never printed or written to the
    report.
  - Only responses whose bytes look like a PDF are saved.
"""

import argparse
import ast
import csv
import http.cookiejar
import os
import re
import stat
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ADMIN_HOST = "www.scaler.com"
ADMIN_PATH = "/admin/fetch-resume/"
ASSET_URL_RE = re.compile(
    r"https://moonshot\.scaler\.com/asset/precomputed-asset/[0-9a-f]{32,128}"
)
MAX_REDIRECTS = 10
TIMEOUT = (10, 45)  # (connect, read) seconds

# Browser headers that break scripted downloads or must never be replayed.
DROP_HEADERS = {
    "cookie", "host", "content-length", "accept-encoding", "range",
    "if-none-match", "if-modified-since", "if-range",
}
# The only headers sent to hosts other than ADMIN_HOST.
OFF_ORIGIN_HEADERS = {"user-agent", "accept-language"}

REPORT_COLUMNS = ["Email", "Resume URL", "Status", "File", "Content-Type", "Error"]

# One quoted shell argument: $'ansi-c', 'single' or "double".
QUOTED = r"""(?:\$'((?:\\.|[^'\\])*)'|'([^']*)'|"((?:\\.|[^"\\])*)")"""


class DownloadError(Exception):
    def __init__(self, message, status="FAILED", auth=False):
        super().__init__(message)
        self.status = status
        self.auth = auth


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

def _unquote(ansi, single, double):
    """Decode one shell-quoted argument captured by QUOTED."""
    if ansi is not None:
        try:
            return ast.literal_eval("'" + ansi + "'")
        except (SyntaxError, ValueError) as exc:
            raise ValueError("Could not decode a $'...' cURL argument.") from exc
    if single is not None:
        return single  # Bash single quotes have no escapes.
    return re.sub(r'\\(["\\$`])', r"\1", double)


def parse_curl_file(path):
    """Return (headers, cookie) from a 'Copy as cURL' file."""
    path = Path(path)
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {path} holds a live admin session and is readable by "
            f"other users. Run: chmod 600 {path}",
            file=sys.stderr,
        )
    text = path.read_text(encoding="utf-8")

    url_match = re.search(r"(?:^|\s)curl\s+" + QUOTED, text)
    if not url_match:
        raise ValueError("Could not find the URL in the cURL file.")
    parsed = urlparse(_unquote(*url_match.groups()))
    if (parsed.scheme, parsed.hostname, parsed.path) != ("https", ADMIN_HOST, ADMIN_PATH):
        raise ValueError(f"The cURL URL must be https://{ADMIN_HOST}{ADMIN_PATH}")

    headers, header_cookie = {}, None
    for m in re.finditer(r"(?:^|\s)(?:-H|--header)\s+" + QUOTED, text):
        header = _unquote(*m.groups())
        if ":" not in header:
            continue
        name, value = (part.strip() for part in header.split(":", 1))
        if name.lower() == "cookie":
            header_cookie = value
        elif name.lower() not in DROP_HEADERS:
            headers[name] = value

    cookie_match = re.search(r"(?:^|\s)(?:-b|--cookie)\s+" + QUOTED, text)
    cookie = _unquote(*cookie_match.groups()) if cookie_match else header_cookie
    if not cookie or not cookie.strip():
        raise ValueError("No cookie found (looked for -b/--cookie and a Cookie header).")

    return headers, cookie


def load_learners(path):
    """Return (learners, invalid_rows) from a CSV/TSV file."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        if Path(path).suffix.lower() == ".tsv":
            dialect = csv.excel_tab
        else:
            sample = fh.read(8192)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            except csv.Error:
                dialect = csv.excel

        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("The input file has no header row.")
        reader.fieldnames = [name.strip() for name in reader.fieldnames]

        required = {"Email", "Resume URL (default)"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError("Missing required column(s): " + ", ".join(sorted(missing)))

        learners, invalid = [], []
        for row in reader:
            email = (row.get("Email") or "").strip()
            url = (row.get("Resume URL (default)") or "").strip()
            if email and url:
                learners.append({"email": email, "url": url})
            elif email or url:
                invalid.append(report_row(
                    email, url, "INVALID_ROW",
                    error="Missing resume URL" if email else "Missing email",
                ))
    return learners, invalid


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    # Never store or replay cookies the servers set; the cookie is explicit.
    session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def headers_for(host, headers, cookie):
    if host == ADMIN_HOST:
        out = dict(headers)
        out["Cookie"] = cookie
        return out
    return {k: v for k, v in headers.items() if k.lower() in OFF_ORIGIN_HEADERS}


def fetch(session, url, headers, cookie):
    """GET url, following HTTPS redirects without leaking credentials.

    Returns (response, final_host).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https":
            raise DownloadError("Refusing a non-HTTPS URL or redirect.")
        resp = session.get(
            current,
            headers=headers_for(parsed.hostname, headers, cookie),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        if resp.is_redirect:
            location = resp.headers["Location"]
            resp.close()
            current = urljoin(current, location)
            continue
        return resp, parsed.hostname
    raise DownloadError(f"Exceeded {MAX_REDIRECTS} redirects.")


def looks_like_pdf(data):
    return b"%PDF-" in data[:1024]


def atomic_write(destination, data):
    tmp = destination.with_name(destination.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, destination)


def poll_asset(session, asset_url, headers, cookie, destination, poll_timeout):
    """Poll the precomputed-asset URL until it serves a PDF, then save it."""
    deadline = time.monotonic() + poll_timeout
    delay, last = 1.0, "no response"
    while True:
        resp, host = fetch(session, asset_url, headers, cookie)
        try:
            if resp.status_code == 200:
                body = resp.content
                ctype = resp.headers.get("Content-Type", "")
                if not looks_like_pdf(body):
                    raise DownloadError(
                        f"File is not a PDF (Content-Type: {ctype or 'unknown'})",
                        status="NOT_PDF",
                    )
                atomic_write(destination, body)
                return ctype
            last = f"HTTP {resp.status_code} from {host}"
        finally:
            resp.close()

        if time.monotonic() + delay > deadline:
            raise DownloadError(f"Asset not ready after {poll_timeout:.0f}s ({last})")
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)


def download_one(session, url, headers, cookie, destination, poll_timeout):
    """Download one resume to destination. Returns its Content-Type."""
    parsed = urlparse(url)
    if (parsed.scheme, parsed.hostname, parsed.path) != ("https", ADMIN_HOST, ADMIN_PATH):
        raise DownloadError("Resume URL is not an expected Scaler admin URL.")

    resp, _ = fetch(session, url, headers, cookie)
    try:
        if resp.status_code in (401, 403):
            raise DownloadError(f"HTTP {resp.status_code} from admin endpoint", auth=True)
        if resp.status_code != 200:
            raise DownloadError(f"HTTP {resp.status_code} from admin endpoint")
        body = resp.content
        ctype = resp.headers.get("Content-Type", "")
    finally:
        resp.close()

    if looks_like_pdf(body):
        atomic_write(destination, body)
        return ctype

    match = ASSET_URL_RE.search(body.decode("utf-8", "replace"))
    if not match:
        raise DownloadError(
            "Admin endpoint returned a page without an asset link "
            "(session expired, or the page format changed).",
            auth=True,
        )
    return poll_asset(session, match.group(0), headers, cookie, destination, poll_timeout)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def safe_filename(email):
    return re.sub(r"[^A-Za-z0-9@._+-]", "_", email)[:200]


def report_row(email, url, status, file="", content_type="", error=""):
    return {
        "Email": email, "Resume URL": url, "Status": status,
        "File": file, "Content-Type": content_type, "Error": error,
    }


def has_valid_pdf(path):
    try:
        with open(path, "rb") as fh:
            return looks_like_pdf(fh.read(1024))
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Download Scaler resumes listed in a CSV/TSV file.")
    parser.add_argument("--curl-file", default="request.curl", help="File with the complete Copy as cURL command.")
    parser.add_argument("--input", default="learners.tsv", help="CSV/TSV with Email and Resume URL (default) columns.")
    parser.add_argument("--output-dir", default="resumes", help="Directory for the PDF files.")
    parser.add_argument("--report", default="download_report.csv", help="CSV report path.")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between learners (default: 0.5).")
    parser.add_argument("--poll-timeout", type=float, default=90, help="Seconds to wait for each file to be generated (default: 90).")
    parser.add_argument("--max-auth-failures", type=int, default=3, help="Stop after this many consecutive session failures (default: 3).")
    parser.add_argument("--force", action="store_true", help="Re-download files that already exist.")
    args = parser.parse_args()

    try:
        headers, cookie = parse_curl_file(args.curl_file)
        learners, invalid_rows = load_learners(args.input)
    except (OSError, ValueError) as exc:
        sys.exit(f"Input error: {exc}")
    if not learners:
        sys.exit("No learners with both email and resume URL were found.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    session = make_session()
    counts = Counter()
    seen = set()
    consecutive_auth = 0
    total = len(learners)

    with report_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()

        def record(row):
            writer.writerow(row)
            fh.flush()
            counts[row["Status"]] += 1

        for row in invalid_rows:
            record(row)

        try:
            for index, learner in enumerate(learners, start=1):
                email, url = learner["email"], learner["url"]
                print(f"[{index}/{total}] {email} ... ", end="", flush=True)

                key = email.lower()
                if key in seen:
                    print("DUPLICATE (skipped)")
                    record(report_row(email, url, "DUPLICATE", error="Email already processed"))
                    continue
                seen.add(key)

                destination = output_dir / (safe_filename(email) + ".pdf")
                if not args.force and has_valid_pdf(destination):
                    print("already downloaded")
                    record(report_row(email, url, "SKIPPED_EXISTING", file=str(destination)))
                    continue

                try:
                    ctype = download_one(session, url, headers, cookie, destination, args.poll_timeout)
                    consecutive_auth = 0
                    print("SUCCESS")
                    record(report_row(email, url, "SUCCESS", file=str(destination), content_type=ctype))
                except DownloadError as exc:
                    print(f"{exc.status}: {exc}")
                    record(report_row(email, url, exc.status, error=str(exc)))
                    consecutive_auth = consecutive_auth + 1 if exc.auth else 0
                    if consecutive_auth >= args.max_auth_failures:
                        print(
                            f"\nStopping: {consecutive_auth} consecutive session failures. "
                            "Copy a fresh cURL from an admin tab and rerun; finished "
                            "files will be skipped."
                        )
                        break
                except requests.RequestException as exc:
                    # Messages from requests can embed signed URLs; log only class and host.
                    req_url = getattr(exc.request, "url", None) if exc.request else None
                    host = urlparse(req_url).hostname if req_url else "unknown host"
                    msg = f"{type(exc).__name__} talking to {host}"
                    print(f"FAILED: {msg}")
                    record(report_row(email, url, "FAILED", error=msg))
                except OSError as exc:
                    msg = f"Could not write file: {exc.strerror}"
                    print(f"FAILED: {msg}")
                    record(report_row(email, url, "FAILED", error=msg))

                if index < total and args.delay > 0:
                    time.sleep(args.delay)
        except KeyboardInterrupt:
            print("\nInterrupted. The report covers everything processed so far.")

    print("\nDownload summary")
    for status, n in sorted(counts.items()):
        print(f"  {status:<17} {n}")
    print(f"Report: {report_path.resolve()}")
    print(f"PDFs:   {output_dir.resolve()}")


if __name__ == "__main__":
    main()