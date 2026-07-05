#!/bin/bash
# 数据源全面连通性测试
# 用法: bash scripts/test_all_sources.sh
set -e
PROJECT=/Users/mariusto/project/quant
V12=$PROJECT/.venv-tushare/bin/python3
V14=$PROJECT/.venv/bin/python3

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
df2 = rs2.get_data()
print(f"行业分类: {len(df2)} rows")

rs3 = bs.query_stock_basic()
df3 = rs3.get_data()
print(f"股票列表: {len(df3)} rows")

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
import akshare as ak
df = ak.stock_info_a_code_name()
print(f"stock list: {len(df)} rows")
df = ak.stock_zh_a_hist("000001", "daily", "20260701", "20260703", "qfq")
print(f"daily OHLCV (000001): {len(df)} rows")
df = ak.stock_lhb_detail_em("20260701", "20260703")
print(f"lhb detail: {len(df)} rows")
try:
    df = ak.stock_margin_detail_sse(date="20260703")
    print(f"margin SSE: {len(df)} rows")
except Exception as e:
    print(f"margin SSE: FAIL ({e})")
try:
    df = ak.stock_hsgt_individual_em(stock="600519")
    print(f"northbound: {len(df)} rows")
except Exception as e:
    print(f"northbound: FAIL ({e})")
try:
    df = ak.stock_individual_fund_flow(stock="000001", market="sz")
    print(f"fund flow: {len(df)} rows")
except Exception as e:
    print(f"fund flow: FAIL ({e})")
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
