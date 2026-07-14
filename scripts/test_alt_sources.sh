#!/bin/bash
echo "=== Eastmoney K线 on 82 子域 ==="
curl -s --max-time 8 -o /tmp/em82.txt -w "HTTP %{http_code}\n" \
  'https://82.push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&beg=20260701&end=20260715&fields1=f1,f2&fields2=f51,f52&ut=test'
head -c 300 /tmp/em82.txt 2>/dev/null && echo ""

echo ""
echo "=== 腾讯行情 K线 ==="
curl -s --max-time 8 -o /tmp/tx.txt -w "HTTP %{http_code}\n" \
  'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,2026-07-01,,5,qfq'
head -c 300 /tmp/tx.txt 2>/dev/null && echo ""

rm -f /tmp/em82.txt /tmp/tx.txt
