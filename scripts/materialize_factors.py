"""因子物化 — 独立 CLI 工具。

因子回测和策略回测共享的数据准备步骤。
因子值物化一次, 两个回测各自多次消费。

用法:
  PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py [start] [end] [--factors f1,f2,...]

示例:
  # 物化所有 backtesting 因子 (默认 120 天)
  PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py

  # 指定日期范围和因子
  PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py 2026-01-01 2026-07-15 --factors dt_streak,roe_trimmed

  # 全量重建
  PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py --force
"""
import sys, os
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
sys.path.insert(0, _root)

from quant.utils.excepthook import setup; setup()
from quant.utils.logger import get_logger, set_trace_id
from quant.factor.store import FactorStore
from quant.factor.compute._registry import get_factor_names
from quant.data.store import DataStore, market_conn
from quant.data.repos import UniverseRepo
from quant.config.constants import _require_cfg
import uuid as _uuid

tid = _uuid.uuid4().hex[:12]
set_trace_id(tid)
log = get_logger("materialize")

# ── 解析参数 ──
import argparse
p = argparse.ArgumentParser(description="因子值物化")
p.add_argument("start", nargs="?", default=None, help="开始日期 YYYY-MM-DD")
p.add_argument("end", nargs="?", default=None, help="结束日期 YYYY-MM-DD")
p.add_argument("--factors", default=None, help="因子名, 逗号分隔 (默认: 全部 backtesting)")
p.add_argument("--symbols", type=int, default=None, help="股票池大小 (默认: 配置的 backtest.universe_size)")
p.add_argument("--force", action="store_true", help="强制重建")
p.add_argument("--from-evaluation", action="store_true", help="从 evaluation_runs 读取通过评估的因子")
p.add_argument("--status", action="store_true", help="仅查看覆盖率")
args = p.parse_args()

store = DataStore()
fs = FactorStore()

# ── 日期范围 ──
if args.start and args.end:
    start, end = args.start, args.end
else:
    import pandas as pd
    # 默认: 最近 120 个自然日对应的交易日
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=180)).strftime("%Y-%m-%d")

from quant.execution.calendar import is_trading_day
import pandas as pd
all_dates = pd.date_range(start=start, end=end, freq="B")
date_range = [d.strftime("%Y-%m-%d") for d in all_dates if is_trading_day(d.date())]
log.info("date range: %s → %s (%d trading days)", date_range[0], date_range[-1], len(date_range))

# ── 因子列表 ──
if args.factors:
    factor_names = [f.strip() for f in args.factors.split(",")]
elif args.from_evaluation:
    from quant.backtest.bridge import evaluation_to_backtest
    factor_names, _ic_map = evaluation_to_backtest()
    log.info("factors: %d (from evaluation_runs)", len(factor_names))
else:
    factor_names = get_factor_names(status_filter="backtesting")
    log.info("factors: %d (status_filter=backtesting)", len(factor_names))

# ── 股票池 ──
n_syms = args.symbols or _require_cfg("factor.evaluation.n_symbols")
# 统一符号选择: UniverseRepo (按流动市值排名), 与 loop.py / smoke_test.py / diagnostics.py 一致
repo = UniverseRepo()
symbols = repo.get_symbols()[:min(n_syms, 6000)]
log.info("symbols: %d (via UniverseRepo)", len(symbols))

# ── 状态模式 ──
if args.status:
    cov = fs.get_coverage(date_range, factor_names)
    print(f"\nCoverage: {cov['covered_dates']}/{cov['total_dates']} dates ({cov['coverage_pct']}%)")
    latest = fs.get_latest_materialization()
    if latest:
        print(f"Latest: {latest['run_ts']} | {latest['date_start']}→{latest['date_end']} | "
              f"{latest['n_factors']} factors × {latest['n_symbols']} symbols × {latest['n_dates']} dates | "
              f"{latest['elapsed_sec']}s | rows={latest['n_rows']:,}")
    sys.exit(0)

# ── 执行物化 ──
log.info("=" * 60)
log.info("MATERIALIZE START: %d factors × %d symbols × %d dates",
         len(factor_names), len(symbols), len(date_range))
log.info("=" * 60)

result = fs.materialize(date_range, factor_names, symbols, store=store, force=args.force)

log.info("=" * 60)
if result.get("skipped"):
    log.info("MATERIALIZE SKIPPED: already up to date")
else:
    log.info("MATERIALIZE DONE: %d rows in %.1fs", result.get("n_rows", 0), result.get("elapsed_sec", 0))
log.info("=" * 60)

fs.close()
store.close()

if result.get("skipped"):
    print("\n[materialize] skipped — already up to date")
else:
    print(f"\n[materialize] done: {result.get('n_rows', 0):,} rows in {result.get('elapsed_sec', 0):.1f}s")
