#!/bin/bash
echo "=== Test 1: Force TLS 1.2 ==="
curl -s -o /tmp/em12.txt -w "HTTP %{http_code}\n" --tlsv1.2 --tls-max 1.2 --connect-timeout 10 \
  'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&beg=20260701&end=20260715&fields1=f1,f2&fields2=f51,f52&ut=test'
head -c 300 /tmp/em12.txt 2>/dev/null && echo ""

echo ""
echo "=== Test 2: Alternative domain (82.push2.eastmoney.com) ==="
curl -s -o /dev/null -w "HTTP %{http_code}\n" --connect-timeout 10 \
  'https://82.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=0&np=1&fltt=2&fields=f12&fs=m:0+t:6'

echo ""
echo "=== Test 3: Sina finance (alternative source) ==="
curl -s -o /tmp/sina.txt -w "HTTP %{http_code}\n" --connect-timeout 10 \
  'https://hq.sinajs.cn/list=sh600519'
head -c 200 /tmp/sina.txt 2>/dev/null && echo ""

echo ""
echo "=== Test 4: Tencent finance ==="
curl -s -o /tmp/tencent.txt -w "HTTP %{http_code}\n" --connect-timeout 10 \
  'https://qt.gtimg.cn/q=sh600519'
head -c 200 /tmp/tencent.txt 2>/dev/null && echo ""

rm -f /tmp/em12.txt /tmp/sina.txt /tmp/tencent.txt
