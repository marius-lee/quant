#!/bin/bash
cd /Users/mariusto/project/quant
PYTHONPATH=. .venv/bin/python << 'PYEOF'
import requests

# 东方财富 K 线 API — akshare 同款 URL
url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
params = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
    "klt": "101",
    "fqt": "1",
    "secid": "1.600519",
    "beg": "20260701",
    "end": "20260715",
}

print("Testing requests directly...")
try:
    r = requests.get(url, params=params, timeout=15)
    print(f"status={r.status_code} len={len(r.text)}")
    if r.status_code == 200:
        data = r.json()
        klines = data.get("data", {}).get("klines", [])
        print(f"klines: {len(klines)} rows")
        for k in klines[-3:]:
            print(f"  {k}")
    else:
        print(f"body: {r.text[:300]}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")

# Also try with urllib3 to check TLS
print("\nTesting with urllib3...")
try:
    import urllib3
    urllib3.disable_warnings()
    http = urllib3.PoolManager()
    r = http.request("GET", url, fields=params, timeout=15.0)
    print(f"status={r.status} len={len(r.data)}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")

# Try HTTP version
print("\nTesting HTTP (not HTTPS)...")
try:
    url_http = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    r = requests.get(url_http, params=params, timeout=15)
    print(f"status={r.status_code} len={len(r.text)}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
PYEOF
