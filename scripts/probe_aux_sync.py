"""fund_flow / margin 同步代码路径探针 — 审计 P0-2 (2026-07-26).

验证: ① 东财 fund_flow API python requests 路径是否可用 (沙箱内 RemoteDisconnected,
curl 却 200 — 需真实终端判定); ② margin SSE+SZSE 单日增量。

用法: .venv/bin/python scripts/probe_aux_sync.py
"""
import sqlite3

from quant.data.fund_flow import sync_single_stock
from quant.data import margin

print("── fund_flow 000001 探针 ──")
n = sync_single_stock("000001")
print(f"rows written: {n}")

print("── margin 2026-07-24 单日探针 ──")
m = margin.sync_range("2026-07-24", "2026-07-24")
print(f"rows written: {m}")

c = sqlite3.connect("quant/data/market.db", timeout=30)
for t in ("fund_flow", "margin_detail"):
    try:
        print(t, c.execute(f"SELECT COUNT(*), MAX(date) FROM {t}").fetchone())
    except Exception as e:
        print(t, "ERR", e)
c.close()
