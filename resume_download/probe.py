# probe.py
from urllib.parse import urlparse
from fetch_resumes import parse_curl_file, load_learners, fetch_response

_, headers, cookie = parse_curl_file("request.curl")
url = load_learners("learners.tsv")[0]["url"]

print("cookie length:", len(cookie), "| cookie names:", [c.split("=")[0].strip() for c in cookie.split(";")][:10])
print("header names:", sorted(headers))

for label, h in [("as copied", headers),
                 ("no accept-encoding", {k: v for k, v in headers.items() if k.lower() != "accept-encoding"})]:
    r = fetch_response(url, h, cookie)
    print(f"\n--- {label} ---")
    print("status:", r.status_code, "| final host:", urlparse(r.url).hostname)
    print("content-type:", r.headers.get("Content-Type"))
    print("content-encoding:", r.headers.get("Content-Encoding"))
    print("first 300 bytes:", r.content[:300])
    r.close()