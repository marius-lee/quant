"""测试数据源连通性 — 腾讯 + akshare (eastmoney)"""
import requests, json

def test_source(name, url):
    try:
        r = requests.get(url, timeout=10)
        data = r.json().get("data", {})
        rows = data.get("klines", [])
        print(f"{name}: HTTP {r.status_code}, rows={len(rows)}")
        if rows:
            print(f"  sample: {rows[0]} ... {rows[-1]}")
    except Exception as e:
        print(f"{name}: FAILED — {e}")

# 腾讯源 (82 子域, qfq 前复权)
tencent_url = (
    "https://82.push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.600519&klt=101&fqt=1&beg=20260710&end=20260716"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116"
    "&ut=7eea3edcaed734bea9cbfc24409ed989"
)
test_source("腾讯源(82子域)", tencent_url)

# akshare 源 (eastmoney 主域)
akshare_url = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.600519&klt=101&fqt=1&beg=20260710&end=20260716"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116"
    "&ut=7eea3edcaed734bea9cbfc24409ed989"
)
test_source("akshare源(eastmoney主域)", akshare_url)
