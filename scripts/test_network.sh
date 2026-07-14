#!/bin/bash
URL='https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&beg=20260701&end=20260715'
echo "testing eastmoney API..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL")
echo "HTTP status: $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
    echo "eastmoney API reachable"
else
    echo "eastmoney API unreachable (code=$HTTP_CODE) — likely network/VPN/firewall issue"
fi
