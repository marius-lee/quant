"""对比测试: SH vs SZ, 大行 vs 小盘, lmt=0 vs lmt=100."""
import requests, json, time

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

tests = [
    ("000001", "0.000001", "SZ平安银行"),
    ("601398", "1.601398", "SH工商银行(大行)"),
    ("600519", "1.600519", "SH贵州茅台"),
    ("603160", "1.603160", "SH汇顶科技(小盘)"),
]

for symbol, secid, label in tests:
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        f"?lmt=100&klt=101&secid={secid}"
        f"&fields1=f1,f2,f3,f7"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        f"&ut=b2884a393a59ad64002292a3e90d46a5"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        data = resp.json()
        name = data['data'].get('name', '?')
        n = len(data['data'].get('klines', []))
        print(f"  {label} ({symbol}): OK — {name} {n}行")
    except Exception as e:
        print(f"  {label} ({symbol}): FAIL — {type(e).__name__}")

    time.sleep(2)

print()
print("lmt=100 限制最近100条 — 如果全部成功说明数据量是问题")
