"""测试东方财富 fund_flow API 连通性 — curl 直连."""
import subprocess, sys

url = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?lmt=0&klt=101&secid=0.000001"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)

# Python requests — same env as akshare
import urllib.request
try:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read()[:200]
    print(f"HTTP {resp.getcode()}: {data.decode('utf-8', errors='replace')[:150]}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
