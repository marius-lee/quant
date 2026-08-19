"""实测早间链 (daily_repair) 完整耗时 — 待处理表 + 补拉 + 重审计.

用途: 量化 08:00 早间链兜底任务需要多久, 评估 30min 窗口是否够用,
      以及是否需将开始时间提前 (如 06:00)。

用法: PYTHONPATH=. .venv/bin/python scripts/measure_daily_repair.py [today]
      today 默认取当前日期; 可选传 2026-08-20 指定日期。

幂等性: repair_and_reaudit 只做补拉 + 重审计 (data_audit 标记 repaired),
        不重复写数据 (各表 sync_main 均为 UPSERT/区间补拉), 可重复执行。
注意: 请避开 08:00-08:30 早间链窗口运行, 避免与生产调度并发抢 baostock 锁。
"""
import sys
import time

from quant.scheduler.repair import _pending_tables
from quant.data.data_health import repair_and_reaudit

today = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")

t0 = time.time()
tables = _pending_tables(today)
print(f"[{today}] 待处理 {len(tables)} 张表: {tables}", flush=True)
if not tables:
    print(f"[{today}] 无待修复表 — 早间链空转, 0s", flush=True)
    sys.exit(0)

repaired, still = repair_and_reaudit(today, tables)
elapsed = time.time() - t0
print(f"[{today}] 修复完成: repaired={repaired}", flush=True)
print(f"[{today}] 仍失败:    {still}", flush=True)
print(f"[{today}] 早间链完整耗时: {elapsed:.1f}s ({elapsed / 60:.1f}min)", flush=True)