#!/bin/bash
echo "=== DNS 解析 ==="
echo -n "82.push2his.eastmoney.com: "
dig +short 82.push2his.eastmoney.com 2>/dev/null || nslookup 82.push2his.eastmoney.com 2>/dev/null | grep Address | tail -1 || echo "DNS FAILED"

echo -n "push2his.eastmoney.com: "
dig +short push2his.eastmoney.com 2>/dev/null || nslookup push2his.eastmoney.com 2>/dev/null | grep Address | tail -1 || echo "DNS FAILED"

echo ""
echo "=== curl 测试 ==="
URL="https://82.push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000004&klt=101&fqt=1&beg=20260714&end=20260716&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116&ut=7eea3edcaed734bea9cbfc24409ed989"
HTTP=$(curl -s -o /tmp/_kline_out.json -w "%{http_code}" --connect-timeout 10 "$URL")
echo "HTTP $HTTP"
if [ -s /tmp/_kline_out.json ]; then
    python3 -c "import json; d=json.load(open('/tmp/_kline_out.json')); print(f'rows={len(d.get(\"data\",{}).get(\"klines\",[]))}')" 2>/dev/null
fi
rm -f /tmp/_kline_out.json

echo ""
echo "=== /etc/hosts 检查 ==="
grep -i eastmoney /etc/hosts 2>/dev/null || echo "(none)"
