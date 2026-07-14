#!/bin/bash
# 测试东方财富 HTTPS API
URL='https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&beg=20260701&end=20260715'

echo "=== DNS ==="
nslookup push2his.eastmoney.com 2>&1 | head -10

echo ""
echo "=== HTTPS (url in temp file to avoid shell escaping) ==="
HTTP_CODE=$(curl -s -o /tmp/em_resp.txt -w "%{http_code}" --connect-timeout 15 "$URL")
echo "HTTP status: $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
    echo "SUCCESS — response preview:"
    head -c 500 /tmp/em_resp.txt
else
    echo "FAILED — curl verbose:"
    curl -v --connect-timeout 15 "$URL" 2>&1 | head -30
fi
rm -f /tmp/em_resp.txt

echo ""
echo "=== akshare quick test (single stock) ==="
cd /Users/mariusto/project/quant && PYTHONPATH=. .venv/bin/python -c "
import akshare as ak
try:
    df = ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20260701', end_date='20260715', adjust='qfq')
    print(f'Got {len(df)} rows for 600519')
    print(df.tail(3))
except Exception as e:
    print(f'akshare error: {type(e).__name__}: {e}')
" 2>&1
