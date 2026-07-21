"""诊断 sina K线接口返回字段 — 确认是否有 turnover 列"""
import urllib.request, json

url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
       "CN_MarketData.getKLineData?symbol=sh600519&scale=240&datalen=5")
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn",
})
data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
if data:
    print("字段列表:", list(data[0].keys()))
    print("最新一条:", json.dumps(data[-1], ensure_ascii=False))
else:
    print("EMPTY")
