# probe2.py
import re, time, requests
from fetch_resumes import parse_curl_file, load_learners, fetch_response

_, headers, cookie = parse_curl_file("request.curl")
url = load_learners("learners.tsv")[0]["url"]

r = fetch_response(url, headers, cookie)
html = r.text
r.close()

# Show the polling script with the asset hash masked
print(re.sub(r"[0-9a-f]{40,}", "<HASH>", html))

m = re.search(r"https://moonshot\.scaler\.com/asset/precomputed-asset/[0-9a-f]+", html)
asset = m.group(0)
ua = {"User-Agent": headers.get("user-agent", "")}

for i in range(15):
    a = requests.get(asset, headers=ua, timeout=(10, 45), allow_redirects=False)
    print(f"\npoll {i}: status={a.status_code} type={a.headers.get('Content-Type')} "
          f"location_host={requests.utils.urlparse(a.headers.get('Location','')).hostname}")
    print("first 200 bytes:", a.content[:200])
    if a.status_code != 202 and a.status_code < 400:
        break
    time.sleep(2)