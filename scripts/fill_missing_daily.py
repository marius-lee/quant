"""补拉 daily 缺失数据 — 通过 curl 调用 gtimg (绕过 Python DNS)"""
import subprocess, json, sqlite3, time

DB = "quant/data/market.db"
DATES = ["2026-07-14", "2026-07-15", "2026-07-16"]

def gtimg_market(sym):
    if sym.startswith("6"):
        return "sh"
    return "sz"

conn = sqlite3.connect(DB)
all_non_bj = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market!='BJ'").fetchall()}
s16 = {r[0] for r in conn.execute("SELECT symbol FROM daily WHERE date='2026-07-16'").fetchall()}
missing = sorted(all_non_bj - s16)
print(f"缺失股票: {len(missing)} 只")

if not missing:
    conn.close()
    exit()

total_new = 0
for i, sym in enumerate(missing):
    pfx = gtimg_market(sym)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={pfx}{sym},day,2026-07-14,2026-07-16,10,qfq")

    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "8",
             "-H", "Referer: https://gu.qq.com/", url],
            capture_output=True, text=True, timeout=12
        )
        if r.returncode != 0 or not r.stdout.strip():
            print(f"  {sym}: HTTP fail (code={r.returncode})")
            time.sleep(0.5)
            continue

        data = json.loads(r.stdout)
        day_list = data.get("data", {}).get(f"{pfx}{sym}", {}).get("qfqday", [])

        new = 0
        for row in day_list:
            d = row[0]
            if d not in DATES:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO daily
                       (symbol,date,open,high,low,close,volume)
                       VALUES (?,?,?,?,?,?,?)""",
                    (sym, d,
                     float(row[1]), float(row[3]), float(row[4]),
                     float(row[2]), int(float(row[5])))
                )
                new += conn.execute("SELECT changes()").fetchone()[0]
            except Exception:
                pass

        if new:
            total_new += new
            print(f"  {sym}: {new} rows")
    except Exception as e:
        print(f"  {sym}: FAILED — {e}")
        time.sleep(0.5)

    if (i + 1) % 20 == 0:
        conn.commit()
        print(f"  [{i+1}/{len(missing)}] {total_new} new rows")

conn.commit()

# Final stats
s16_new = conn.execute("SELECT COUNT(*) FROM daily WHERE date='2026-07-16'").fetchone()[0]
print(f"\nDone: {total_new} rows, 07-16 now has {s16_new} symbols")
conn.close()
