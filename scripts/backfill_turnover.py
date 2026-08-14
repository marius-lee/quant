"""存量 daily.turnover 回填入口 — 复用 DataStore.backfill_turnover 成熟逻辑.

背景: _fetch_tushare_daily 写的 daily.turnover 恒为 0 (tushare daily API 不含
turnover_rate, 2026-07-21 实测); 2020-2024 全市场 ~99.9% 行 turnover=0,
导致 turnover 系因子 (turnover_accel/ctr_20d/abn_turnover 等 10 个) 在受影响
日期永远物化不出数据. full 模式每 symbol 一次 baostock 查询全区间, 5208 只
≈ 40 分钟 (逐日模式需 7.2M 次查询不可行).

用法:
    PYTHONPATH=. .venv/bin/python scripts/backfill_turnover.py            # 全量 (full)
    PYTHONPATH=. .venv/bin/python scripts/backfill_turnover.py --date 2026-08-12  # 单日
"""
import argparse
import sys
import time

from quant.data.store import DataStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="只回填该日缺口 (逐日模式)")
    ap.add_argument("--full", action="store_true", help="全量模式 (默认)")
    args = ap.parse_args()

    if not args.date and not args.full:
        args.full = True  # 默认全量 — 存量缺口规模下逐日模式不现实

    s = DataStore()
    _t0 = time.time()
    n = s.backfill_turnover(date=args.date, full=args.full and not args.date)
    el = time.time() - _t0
    print(f"turnover backfill: {n} rows updated in {el/60:.1f}min", flush=True)
    s.close()
    return 0 if args.full or n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())