#!/bin/bash
# 数据源全面连通性测试
# 用法: bash scripts/test_all_sources.sh

PROJECT=/Users/mariusto/project/quant
V12=$PROJECT/.venv-tushare/bin/python3
V14=$PROJECT/.venv/bin/python3


# 自动加载 config/.env 凭证
ENV_FILE="$PROJECT/config/.env"
if [ -f "$ENV_FILE" ]; then
  export $(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)
fi
SEP="============================================================"
echo "$SEP"
echo "数据源连通性测试 — 2026-07-05"
echo "$SEP"

# ═══ baostock (Py3.12) ═══
echo ""
echo "--- baostock ---"
$V12 << 'EOF'
import baostock as bs
lg = bs.login()
print(f"login: {lg.error_code} {lg.error_msg}")
if lg.error_code != "0":
    bs.logout()
    exit(1)
rs = bs.query_history_k_data_plus("sh.600519", "date,code,close,peTTM,pbMRQ",
    start_date="2026-07-01", end_date="2026-07-03", frequency="d", adjustflag="2")
data = []
while rs.next(): data.append(rs.get_row_data())
print(f"K线: {len(data)} rows")

rs2 = bs.query_stock_industry()
rows2 = []
while rs2.next(): rows2.append(rs2.get_row_data())
print(f"行业分类: {len(rows2)} rows")

rs3 = bs.query_stock_basic()
rows3 = []
while rs3.next(): rows3.append(rs3.get_row_data())
print(f"股票列表: {len(rows3)} rows")

bs.logout()
print("baostock: OK")
EOF

# ═══ tushare (Py3.12) ═══
echo ""
echo "--- tushare ---"
$V12 << 'EOF'
import os
token = os.environ.get("TUSHARE_TOKEN", "")
if not token:
    print("tushare: SKIP (TUSHARE_TOKEN 未设置)")
else:
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name", limit=5)
    print(f"stock_basic: {len(df)} rows" if df is not None else "stock_basic: None")
    df2 = pro.daily_basic(ts_code="000001.SZ", trade_date="20260703", fields="ts_code,trade_date,pe_ttm,pb")
    print(f"daily_basic (000001): {len(df2)} rows" if df2 is not None else "daily_basic: None")
    print("tushare: OK")
EOF

# ═══ JQData (Py3.12) ═══
echo ""
echo "--- JQData ---"
$V12 << 'EOF'
import os
user = os.environ.get("JQDATA_USER", "")
pw = os.environ.get("JQDATA_PASS", "")
if not user or not pw:
    print("JQData: SKIP (JQDATA_USER/JQDATA_PASS 未设置)")
else:
    from jqdatasdk import auth, get_fundamentals, query, valuation, logout
    auth(user, pw)
    try:
        q = query(valuation).limit(3)
        df = get_fundamentals(q, date="2026-07-01")
        print(f"valuation: {len(df)} rows" if df is not None else "valuation: None")
    finally:
        logout()
    print("JQData: OK")
EOF


# ═══ akshare (Py3.14) ═══
echo ""
echo "--- akshare ---"
$V14 << 'EOF'
import akshare as ak, time

def try_call(label, fn):
    for attempt in range(1, 4):
        try:
            result = fn()
            n = len(result) if result is not None else 0
            print(f"{label}: {n} rows")
            return result
        except Exception as e:
            if attempt < 3:
                wait = 2 * attempt
                print(f"{label}: retry {attempt}/3 after {wait}s ({type(e).__name__})")
                time.sleep(wait)
            else:
                print(f"{label}: FAIL ({type(e).__name__}: {e})")
    return None

try_call("stock list", lambda: ak.stock_info_a_code_name())
try_call("daily OHLCV (000001)", lambda: ak.stock_zh_a_hist("000001", "daily", "20260701", "20260703", "qfq"))
try_call("lhb detail", lambda: ak.stock_lhb_detail_em("20260701", "20260703"))
try_call("margin SSE", lambda: ak.stock_margin_detail_sse(date="20260703"))
try_call("northbound", lambda: ak.stock_hsgt_individual_em(symbol="600519"))
try_call("fund flow", lambda: ak.stock_individual_fund_flow(stock="000001", market="sz"))
print("akshare: OK")
EOF


# ═══ tencent (Py3.14) ═══
echo ""
echo "--- tencent ---"
$V14 << 'EOF'
from urllib.request import Request, urlopen
from json import loads
url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,500,qfq"
r = urlopen(Request(url, headers={"Referer": "http://gu.qq.com/sh600519"}), timeout=10)
data = loads(r.read())
rows = data["data"]["sh600519"]["qfqday"]
print(f"qfq K线 (600519): {len(rows)} rows")
print("tencent: OK")
EOF


# ═══ sina (Py3.14) ═══
echo ""
echo "--- sina ---"
$V14 << 'EOF'
from urllib.request import Request, urlopen
from json import loads
# 新浪历史K线 — 注意: 返回未复权数据, 除权日单日跳变
url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=3"
try:
    r = urlopen(Request(url, headers={"Referer": "https://finance.sina.com.cn"}), timeout=10)
    data = loads(r.read())
    print(f"K线 (600519 未复权): {len(data)} rows")
    if data:
        print(f"  sample keys: {list(data[0].keys())}")
        print(f"  sample: {data[0]}")
    print("sina: OK")
except Exception as e:
    print(f"sina: FAIL ({type(e).__name__}: {e})")
EOF

# ═══ netease (Py3.14) ═══
echo ""
echo "--- netease ---"
$V14 << 'EOF'
from urllib.request import Request, urlopen
# 网易历史日线 (CSV格式, GBK编码)
url = "http://quotes.money.163.com/service/chddata.html?code=0600519&start=20260701&end=20260703&fields=TCLOSE;HIGH;LOW;TOPEN;CHG;PCHG;TURNOVER;VOTURNOVER;VATURNOVER"
try:
    r = urlopen(Request(url, headers={"Referer": "http://money.163.com"}), timeout=10)
    text = r.read().decode("gbk", errors="replace")
    lines = text.strip().split("\n")
    print(f"K线 (600519): {len(lines)-1} rows (header + data)")
    if len(lines) > 1:
        print(f"  header: {lines[0][:120]}")
        print(f"  row1:   {lines[1][:120]}")
    print("netease: OK")
except Exception as e:
    print(f"netease: FAIL ({type(e).__name__}: {e})")

# 网易实时行情 (JSONP, GBK)
url2 = "http://api.money.126.net/data/feed/0600519,money.api"
try:
    r = urlopen(Request(url2, headers={"Referer": "http://money.163.com"}), timeout=10)
    text = r.read().decode("gbk", errors="replace")
    print(f"realtime: {text[:150]}...")
    print("netease realtime: OK")
except Exception as e:
    print(f"netease realtime: FAIL ({type(e).__name__}: {e})")
EOF



# ═══ 同花顺 (Py3.14) ═══
echo ""
echo "--- 同花顺 (10jqka) ---"
$V14 << 'EOF'
from urllib.request import Request, urlopen
from json import loads as jloads
import time

# 尝试多个端点 — v2已404, 试v6和备用域名
endpoints = [
    ("v6 HTTPS", "https://d.10jqka.com.cn/v6/line/stock_zh_600519_09/last.js"),
    ("v6 HTTP",  "http://d.10jqka.com.cn/v6/line/stock_zh_600519_09/last.js"),
    ("stockpage","https://stockpage.10jqka.com.cn/spService/600519/header/1/"),
]
ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
for label, url in endpoints:
    try:
        r = urlopen(Request(url, headers={"Referer": "http://www.10jqka.com.cn", "User-Agent": ua}), timeout=10)
        text = r.read().decode("gbk", errors="replace")
        print(f"{label}: HTTP {r.status}, {len(text)} chars")
        if len(text) > 100:
            print(f"  preview: {text[:200]}")
        if "{" in text:
            js = text[text.index("{"):text.rindex("}")+1]
            data = jloads(js)
            print(f"  parsed: {list(data.keys())[:5]}")
        print(f"同花顺 ({label}): OK")
        break
    except Exception as e:
        print(f"{label}: FAIL ({type(e).__name__}: {e})")
    time.sleep(1)
else:
    print("同花顺: FAIL (all endpoints unreachable)")
EOF



# ═══ 雪球 (Py3.14) ═══
echo ""
echo "--- 雪球 (xueqiu) ---"
$V14 << 'EOF'
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from json import loads as jloads
import time

cj = CookieJar()
opener = build_opener(HTTPCookieProcessor(cj))
try:
    r = opener.open("https://xueqiu.com", timeout=10)
    cookies = {c.name: c.value for c in cj}
    print(f"cookies: {len(cookies)} 个: {list(cookies.keys())}")
except Exception as e:
    print(f"cookie获取: FAIL ({type(e).__name__}: {e})")
    cookies = {}

if cookies:
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    # K线 — 尝试多种参数格式
    now_ms = int(time.time() * 1000)
    three_days_ago = now_ms - 3 * 86400000
    
    # 格式1: 原始格式
    url_k = f"https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=SH600519&begin={three_days_ago}&period=day&type=before&count=-3&indicator=kline,pe,pb"
    try:
        r = urlopen(Request(url_k, headers={"Referer": "https://xueqiu.com/S/SH600519", "User-Agent": ua, "Cookie": cookie_str}), timeout=10)
        text = r.read().decode()
        print(f"K线: HTTP {r.status}, {len(text)} chars")
        if r.status == 200:
            data = jloads(text)
            items = data.get("data", {}).get("item", [])
            cols = data.get("data", {}).get("column", [])
            print(f"  {len(items)} rows, columns: {cols}")
            if items:
                print(f"  sample: {items[0]}")
            print("雪球 K线: OK")
        else:
            print(f"  body: {text[:200]}")
    except Exception as e:
        print(f"雪球 K线: FAIL ({type(e).__name__}: {e})")
    
    # 股票列表
    url_s = "https://xueqiu.com/service/v5/stock/screener/quote/list?page=1&size=3&order=desc&orderby=percent&order_by=percent&market=CN&type=sh_sz"
    try:
        r = urlopen(Request(url_s, headers={"Referer": "https://xueqiu.com/hq", "User-Agent": ua, "Cookie": cookie_str}), timeout=10)
        if r.status == 200:
            data = jloads(r.read())
            total = data.get("data", {}).get("count", 0)
            items = data.get("data", {}).get("list", [])
            print(f"股票列表: {total} total, {len(items)} items")
            print("雪球 股票列表: OK")
        else:
            print(f"股票列表: HTTP {r.status}, body: {r.read().decode()[:200]}")
    except Exception as e:
        print(f"雪球 股票列表: FAIL ({type(e).__name__}: {e})")
else:
    print("雪球: SKIP (无cookie)")
EOF


# ═══ pytdx (Py3.14) ═══
echo ""
echo "--- pytdx ---"
$V14 << 'EOF'
from pytdx.hq import TdxHq_API
api = TdxHq_API()
if api.connect("180.153.18.170", 7709, time_out=3):
    data = api.get_security_bars(9, 1, "000001", 0, 3)
    print(f"K线 (000001): {len(data) if data else 0} bars")
    api.disconnect()
    print("pytdx: OK")
else:
    print("pytdx: 服务器不可达 (connect failed)")
EOF

# ═══ Summary ═══
echo ""
echo "$SEP"
echo "测试完成。请将以上输出完整复制给我。"
echo "$SEP"
