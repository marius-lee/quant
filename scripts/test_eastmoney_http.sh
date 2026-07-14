#!/bin/bash
URL='http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&beg=20260701&end=20260715'

echo "=== HTTP test ==="
HTTP_CODE=$(curl -s -o /tmp/em_http.txt -w "%{http_code}" --connect-timeout 10 "$URL")
echo "HTTP status: $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
    echo "SUCCESS — sample:"
    head -c 500 /tmp/em_http.txt
    echo ""
else
    echo "response:"
    head -c 200 /tmp/em_http.txt
fi
rm -f /tmp/em_http.txt

echo ""
echo "=== akshare stock_zh_a_hist source code check ==="
grep -n "http://\|https://" /Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/akshare/stock_feature/stock_hist_em.py | head -10
