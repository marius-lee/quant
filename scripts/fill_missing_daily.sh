#!/bin/bash
# 通过 curl 拉取 gtimg 行情，补全 daily 表缺失数据
set -e
cd "$(dirname "$0")/.."

DB="quant/data/market.db"
DATES="2026-07-14 2026-07-15 2026-07-16"

echo "=== 查找缺失股票 ==="
MISSING=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
all_bj = {r[0] for r in c.execute(\"SELECT symbol FROM stocks WHERE market!='BJ'\")}
s16   = {r[0] for r in c.execute(\"SELECT symbol FROM daily WHERE date='2026-07-16'\")}
miss  = sorted(all_bj - s16)
print(' '.join(miss))
c.close()
")

if [ -z "$MISSING" ]; then
    echo "无需补拉"
    exit 0
fi

COUNT=$(echo "$MISSING" | wc -w | tr -d ' ')
echo "缺失: $COUNT 只"
echo ""

TOTAL=0
I=0
for SYM in $MISSING; do
    I=$((I + 1))
    
    # 市场前缀
    if [[ $SYM == 6* ]]; then
        PFX="sh"
    elif [[ $SYM == 0* ]] || [[ $SYM == 3* ]]; then
        PFX="sz"
    else
        PFX="sz"
    fi
    
    RESULT=$(curl -s --connect-timeout 8 \
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${PFX}${SYM},day,2026-07-14,2026-07-16,10,qfq" \
        -H "Referer: https://gu.qq.com/")
    
    if [ -z "$RESULT" ]; then
        echo "  $SYM: curl empty"
        sleep 1
        continue
    fi
    
    echo "$RESULT" | PYTHONPATH=. .venv/bin/python3 -c "
import sys, json, sqlite3
DB = '$DB'
DATES = ['2026-07-14', '2026-07-15', '2026-07-16']
sym = '$SYM'
pfx = '$PFX'
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except:
    print(f'  {sym}: JSON parse failed')
    sys.exit(0)

day_list = data.get('data', {}).get(f'{pfx}{sym}', {}).get('qfqday', [])
if not day_list:
    print(f'  {sym}: no data')
    sys.exit(0)

conn = sqlite3.connect(DB)
new = 0
for row in day_list:
    d = row[0]
    if d not in DATES:
        continue
    # gtimg fields: date, open, close, high, low, volume
    try:
        conn.execute(
            'INSERT OR IGNORE INTO daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)',
            (sym, d, float(row[1]), float(row[3]), float(row[4]), float(row[2]), int(float(row[5])))
        )
        new += conn.execute('SELECT changes()').fetchone()[0]
    except:
        pass
conn.commit()
conn.close()
if new > 0:
    print(f'  {sym}: {new} rows')
" 2>/dev/null
    
    # 每20只打印进度
    if [ $((I % 20)) -eq 0 ]; then
        TOTAL_NEW=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
n = c.execute(\"SELECT COUNT(*) FROM daily WHERE date>='2026-07-14' AND date<='2026-07-16'\").fetchone()[0]
print(n)
c.close()
")
        echo "  [$I/$COUNT] done, total new rows: $TOTAL_NEW"
    fi
    
    sleep 0.08
done

TOTAL_NEW=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
n = c.execute(\"SELECT COUNT(*) FROM daily WHERE date>='2026-07-14' AND date<='2026-07-16'\").fetchone()[0]
print(n)
c.close()
")
S16=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
n = c.execute(\"SELECT COUNT(*) FROM daily WHERE date='2026-07-16'\").fetchone()[0]
print(n)
c.close()
")
echo ""
echo "=== Done ==="
echo "07-14~16 total rows: $TOTAL_NEW"
echo "07-16 symbols: $S16"
