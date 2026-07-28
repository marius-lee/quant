"""fund_flow 数据拉取 — 从市值排名 10 开始, 避开大块头银行股."""
import sqlite3, time
from quant.data.fund_flow import sync_single_stock, _ensure_table, DB_PATH
from quant.config.constants import _require_cfg

conn = sqlite3.connect(DB_PATH)
_ensure_table(conn)

# 前10大市值大部分是银行, 数据量太大服务端断连. 从第10名开始.
symbols = [r[0] for r in conn.execute(
    "SELECT symbol FROM stocks WHERE market IN ('SH','SZ') ORDER BY total_mv DESC LIMIT 100 OFFSET 10"
).fetchall()]

ok = fail = total = 0
for i, sym in enumerate(symbols):
    mkt = 'sh' if sym.startswith(('6', '68')) else 'sz'
    n = sync_single_stock(sym, market=mkt, conn=conn)
    total += n
    if n > 0:
        ok += 1
    else:
        fail += 1
    if (i + 1) % 10 == 0:
        print(f"  [{i+1}/100] ok={ok} fail={fail} rows={total}  (已写入)")
        conn.commit()
    time.sleep(_require_cfg("data.api_delay.fund_flow"))

conn.commit()
conn.close()
print(f"\nDone: {ok} ok, {fail} failed, {total} total rows")
