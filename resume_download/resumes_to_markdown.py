#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf4llm>=0.0.17,<2"]
# ///
"""Convert downloaded resume PDFs to Markdown.

Writes one Markdown file per resume, plus one combined file with a table of
contents. If download_report.csv is present, resumes keep the order and email
addresses from that report; any other PDFs in the folder are added after,
sorted by filename.

Usage:
  uv run resumes_to_markdown.py --input-dir resumes --output-dir resumes_md \
      --combined combined_resumes.md
"""

import argparse
import contextlib
import csv
import io
import re
import sys
from datetime import datetime
from pathlib import Path

import pymupdf
import pymupdf4llm

PICTURE_PLACEHOLDER = re.compile(r"^\**\s*==> picture .*<==\s*\**\s*$", re.IGNORECASE)
BOLD_HEADING = re.compile(r"^(#{1,6}\s+)\*\*(.+?)\*\*\s*$")
HEADING = re.compile(r"^(#{1,6})(?=\s)")
FENCE = re.compile(r"^\s*(```|~~~)")
MIN_TEXT_CHARS = 50  # Below this, treat the PDF as having no extractable text.


class ConversionError(Exception):
    pass


def ordered_pdfs(input_dir, report_path):
    """Return [(pdf_path, label)] in report order, then remaining PDFs by name."""
    pdfs = {p.name: p for p in input_dir.glob("*.pdf")}
    ordered = []
    if report_path and report_path.is_file():
        with report_path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = Path(row.get("File") or "").name
                if name in pdfs:
                    path = pdfs.pop(name)
                    ordered.append((path, (row.get("Email") or "").strip() or path.stem))
    ordered += [(path, path.stem) for _, path in sorted(pdfs.items())]
    return ordered


def clean(md):
    lines = []
    for line in md.replace("\x00", "").splitlines():
        line = line.rstrip()
        if PICTURE_PLACEHOLDER.match(line):
            continue
        lines.append(BOLD_HEADING.sub(r"\1\2", line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _to_markdown(doc, ocr):
    """Run the converter quietly; older pymupdf4llm versions lack use_ocr."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return pymupdf4llm.to_markdown(doc, use_ocr=ocr)
        except TypeError:
            return pymupdf4llm.to_markdown(doc)


def pdf_to_markdown(path, ocr=False):
    """Return (markdown, page_count) for one PDF."""
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise ConversionError("PDF is password-protected")
            pages = doc.page_count
            md = _to_markdown(doc, ocr)
    except ConversionError:
        raise
    except Exception as exc:  # pymupdf raises several types for broken files
        raise ConversionError(f"Could not read PDF: {type(exc).__name__}: {exc}") from exc
    return clean(md), pages


def has_text(md):
    return len(re.sub(r"\W", "", md)) >= MIN_TEXT_CHARS


def demote_headings(md, levels):
    """Push every heading down so a resume nests under its own section heading."""
    out, in_fence = [], False
    for line in md.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = HEADING.match(line)
            if m:
                hashes = m.group(1)
                line = "#" * min(len(hashes) + levels, 6) + line[len(hashes):]
        out.append(line)
    return "\n".join(out)


def escape_md(text):
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)


def main():
    parser = argparse.ArgumentParser(description="Convert resume PDFs to per-resume and combined Markdown.")
    parser.add_argument("--input-dir", default="resumes", help="Folder with the downloaded PDFs.")
    parser.add_argument("--output-dir", default="resumes_md", help="Folder for one .md per resume.")
    parser.add_argument("--combined", default="combined_resumes.md", help="Path of the merged Markdown file.")
    parser.add_argument("--report", default="download_report.csv",
                        help="Download report used for ordering and emails (optional).")
    parser.add_argument("--ocr", action="store_true",
                        help="OCR scanned PDFs (slower; needs Tesseract, e.g. brew install tesseract).")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        sys.exit(f"Input folder not found: {input_dir}")

    items = ordered_pdfs(input_dir, Path(args.report))
    if not items:
        sys.exit(f"No PDF files found in {input_dir}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    combined_path = Path(args.combined)
    combined_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(items)
    sections, toc = [], []
    converted = no_text = failed = 0

    for index, (pdf, label) in enumerate(items, start=1):
        print(f"[{index}/{total}] {label} ... ", end="", flush=True)
        anchor = f"resume-{index}"
        toc.append(f"{index}. [{escape_md(label)}](#{anchor})")
        header = f'<a id="{anchor}"></a>\n\n## {index}. {escape_md(label)}\n\n'

        try:
            md, pages = pdf_to_markdown(pdf, args.ocr)
        except ConversionError as exc:
            failed += 1
            print(f"FAILED: {exc}")
            sections.append(header + f"> **Conversion failed:** {escape_md(str(exc))}  \n"
                                     f"> Source: `{pdf.name}`")
            continue

        meta = f"_Source: `{pdf.name}` · {pages} page{'s' if pages != 1 else ''}_"
        if has_text(md):
            body = md
            converted += 1
            print("OK")
        else:
            body = ("> **No extractable text.** The PDF is probably a scanned image; "
                    + ("OCR found nothing either; open the source file." if args.ocr
                       else "open the source file, or rerun with --ocr."))
            no_text += 1
            print("NO TEXT (scanned?)")

        (output_dir / f"{pdf.stem}.md").write_text(
            f"# {escape_md(label)}\n\n{meta}\n\n{demote_headings(body, 1)}\n", encoding="utf-8"
        )
        sections.append(header + f"{meta}\n\n{demote_headings(body, 2)}")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    combined = (
        f"# Combined resumes\n\n"
        f"_Generated {generated} · {total} resumes · {converted} converted, "
        f"{no_text} without text, {failed} failed_\n\n"
        f"## Contents\n\n" + "\n".join(toc) + "\n\n---\n\n"
        + "\n\n---\n\n".join(sections) + "\n"
    )
    combined_path.write_text(combined, encoding="utf-8")

    print("\nSummary")
    print(f"  Converted:    {converted}")
    print(f"  No text:      {no_text}")
    print(f"  Failed:       {failed}")
    print(f"Per-resume:   {output_dir.resolve()}")
    print(f"Combined:     {combined_path.resolve()}")
    if converted == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()