"""临时数据拉取 — 用 82.push2his.eastmoney.com 绕过 CDN 阻断."""
import requests, sqlite3, time, sys

DB = "quant/data/market.db"
URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
START = sys.argv[1] if len(sys.argv) > 1 else "20260710"
END   = sys.argv[2] if len(sys.argv) > 2 else "20260715"

db = sqlite3.connect(DB)
db.execute("PRAGMA journal_mode=WAL")
stale = [r[0] for r in db.execute(
    "SELECT symbol FROM stocks WHERE symbol NOT LIKE '%BJ%' ORDER BY symbol"
).fetchall()]
print(f"pulling {len(stale)} stocks, {START} → {END}")

count, skip = 0, 0
for i, sym in enumerate(stale):
    code = f"1.{sym}" if sym.startswith("6") else f"0.{sym}"
    try:
        r = requests.get(URL, params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101", "fqt": "1", "secid": code,
            "beg": START, "end": END,
        }, timeout=10)
        if r.status_code == 200 and r.json().get("data", {}).get("klines"):
            for k in r.json()["data"]["klines"]:
                p = k.split(",")
                db.execute(
                    "INSERT OR REPLACE INTO daily(date,symbol,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
                    (p[0], sym, float(p[1]), float(p[2]), float(p[3]),
                     float(p[4]), int(p[5]), float(p[6])))
            count += 1
            if count % 100 == 0:
                db.commit()
    except Exception:
        skip += 1
    if (i + 1) % 500 == 0:
        print(f"  {i+1}/{len(stale)} — {count} updated, {skip} skipped")
    time.sleep(0.05)

db.commit()
db.close()
print(f"done: {count} updated, {skip} skipped")
