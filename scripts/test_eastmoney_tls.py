"""TLS/HTTP 诊断 + cookie 测试 — 完整版"""
import sys, subprocess, re
sys.path.insert(0, ".")

print("=" * 50)
print("Step 1: 获取东方财富 cookie")
print("=" * 50)
rc = subprocess.run(
    ["curl", "-s", "-c", "-", "--connect-timeout", "5", "https://quote.eastmoney.com/"],
    capture_output=True, text=True, timeout=10
)
cookies = {}
for line in rc.stdout.strip().split("\n"):
    if line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) >= 7:
        cookies[parts[5]] = parts[6]
print(f"Cookies: {list(cookies.keys())}")

# Build cookie header
cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

print("\n" + "=" * 50)
print("Step 2: 系统 curl + cookie + HTTP/2")
print("=" * 50)
API_URL = (
    "https://push2.eastmoney.com/api/qt/stock/kline/get"
    "?fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116"
    "&ut=7eea3edcaed734bea9cbfc24409ed989"
    "&klt=101&fqt=1&secid=1.600519"
    "&beg=20260710&end=20260716"
)
hdr_cookie = f"Cookie: {cookie_str}" if cookie_str else ""
rc = subprocess.run(
    ["curl", "-s", "--http2", "--connect-timeout", "5", "-H", hdr_cookie,
     "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
     "-H", "Referer: https://quote.eastmoney.com/",
     API_URL],
    capture_output=True, text=True, timeout=15
)
stdout = rc.stdout.strip()
if stdout:
    try:
        import json
        data = json.loads(stdout)
        klines = data.get("data", {}).get("klines", [])
        print(f"系统 curl: HTTP 200, klines={len(klines)}")
        if klines:
            print(f"  sample: {klines[0][:80]}")
    except json.JSONDecodeError:
        print(f"系统 curl: HTTP 200, body={stdout[:100]}")
else:
    print(f"系统 curl: FAILED — empty response")

print("\n" + "=" * 50)
print("Step 3: curl_cffi + cookie + HTTP/2")
print("=" * 50)
import curl_cffi.requests as cr
PARAMS = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
    "klt": "101", "fqt": "1", "secid": "1.600519",
    "beg": "20260710", "end": "20260716",
}
URL = "https://push2.eastmoney.com/api/qt/stock/kline/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
if cookie_str:
    HEADERS["Cookie"] = cookie_str

for target in ["chrome131", "chrome124"]:
    try:
        session = cr.Session()
        resp = session.get(URL, params=PARAMS, headers=HEADERS, timeout=15,
                          impersonate=target, )
        n = len(resp.json().get("data", {}).get("klines", []))
        print(f"curl_cffi {target}: HTTP {resp.status_code}, klines={n}")
        if n:
            print(f"  sample: {resp.json()['data']['klines'][0][:80]}")
        break
    except Exception as e:
        print(f"curl_cffi {target}: {str(e)[:80]}")
