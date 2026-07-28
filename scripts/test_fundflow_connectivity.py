"""测试 urllib vs requests — 用 000001 (之前成功过) 对比."""
import urllib.request, json, time

symbol = '000001'
secid = '0.000001'
url = (
    f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    f"?lmt=0&klt=101&secid={secid}"
    f"&fields1=f1,f2,f3,f7"
    f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    f"&ut=b2884a393a59ad64002292a3e90d46a5"
)

# Test 1: urllib (current approach)
for attempt in range(3):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        name = data['data'].get('name', '?')
        n = len(data['data'].get('klines', []))
        print(f"urllib: OK — {name} {n}行 (attempt {attempt+1})")
        break
    except Exception as e:
        err = str(e)[:80]
        print(f"urllib: FAIL attempt {attempt+1} — {err}")
        time.sleep(2 ** attempt)

# Test 2: requests with browser-like headers
try:
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        name = data['data'].get('name', '?')
        n = len(data['data'].get('klines', []))
        print(f"requests: OK — {name} {n}行")
    else:
        print(f"requests: HTTP {resp.status_code}")
except Exception as e:
    print(f"requests: FAIL — {type(e).__name__}: {str(e)[:80]}")
