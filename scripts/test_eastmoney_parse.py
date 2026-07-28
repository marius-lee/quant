"""解析东方财富 fund_flow API 响应格式."""
import urllib.request, json

url = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?lmt=0&klt=101&secid=0.000001"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)

req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read())

klines = data.get("data", {}).get("klines", [])
print(f"股票: {data['data'].get('name')} ({data['data'].get('code')})")
print(f"数据条数: {len(klines)}")
print(f"\n字段 (f51-f65):")
print("  f51=日期 f52=收盘价 f53=涨跌幅 f54=主力净流入 f55=主力净占比")
print("  f56=超大单净流入 f57=超大单净占比 f58=大单净流入 f59=大单净占比")
print("  f60=中单净流入 f61=中单净占比 f62=小单净流入 f63=小单净占比")
print(f"\n最新 3 条:")
for line in klines[-3:]:
    parts = line.split(",")
    print(f"  {parts[0]}: close={parts[1]} chg={parts[2]} main_in={parts[3]} main_pct={parts[4]}")
    print(f"    超大={parts[5]}/{parts[6]} 大={parts[7]}/{parts[8]} 中={parts[9]}/{parts[10]} 小={parts[11]}/{parts[12]}")
