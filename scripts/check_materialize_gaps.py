#!/usr/bin/env python3
"""核查因子缓存缺口 — 判定完整版 (v529)

用法: PYTHONPATH=. .venv/bin/python scripts/check_materialize_gaps.py [start] [end]
默认窗口: 物化起点 2020-01-01 → 最新交易日

缺口三类定性 (正式口径, 非猜测):
  1. 真交易日缺口 (非 blocked) → 需物化 (如 2026-08-17 晚间链未跑)
  2. blocked 缺口 → 正常机制: 数据源覆盖起点前被剔 (analyst_forecast 仅 2 期快照,
     fund_hold 2024-12 起, holder_trade 2025-01 起), 非数据缺失
  3. 非交易日缺口 (is_trading_day 判定, calendar.py) → 节假日残留, 噪音, 忽略
      (物化日期源 pd.date_range(freq='B') 含法定节假日所致)

幂等: 纯只读核查, 可重复执行
"""
import json
import os
import sys
from datetime import date

from quant.execution.calendar import is_trading_day
from quant.factor.compute import get_factor_names
from quant.factor.store import FactorStore

START = sys.argv[1] if len(sys.argv) > 1 else "2020-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else date.today().strftime("%Y-%m-%d")

s = FactorStore()
td = s._load_trading_days()
pool = set(get_factor_names(status_filter="backtesting"))
blocked = json.load(open(os.path.join(s._cache_dir, "blocked.json")))

valid = [d for d in td if START <= d <= END]
miss_real, miss_blk = {}, {}
for mf in os.listdir(os.path.join(s._cache_dir, "metadata")):
    if not mf.startswith("factor_") or not mf.endswith(".json"):
        continue
    name = mf[len("factor_"):-len(".json")]
    if name not in pool:
        continue
    dates = set(json.load(open(os.path.join(s._cache_dir, "metadata", mf))).get("dates", []))
    for d in valid:
        if d in dates:
            continue
        if name in blocked.get(d, {}):
            miss_blk.setdefault(d, []).append(name)
        else:
            miss_real.setdefault(d, []).append(name)

real_td = {d: f for d, f in sorted(miss_real.items()) if is_trading_day(date.fromisoformat(d))}
real_hd = {d: f for d, f in sorted(miss_real.items()) if not is_trading_day(date.fromisoformat(d))}

print(f"窗口 {valid[0]} → {valid[-1]} ({len(valid)} 交易日历天 | 池内 {len(pool)} 因子)")
print(f"1. 真交易日缺口 (需物化): {len(real_td)} 天")
for d, fs in real_td.items():
    print(f"     {d}: {len(fs)} 因子 — {','.join(fs[:5])}{'...' if len(fs) > 5 else ''}")
print(f"2. blocked 缺口 (正常机制): {len(miss_blk)} 天")
for d, fs in list(sorted(miss_blk.items()))[:3]:
    print(f"     {d}: {len(fs)} 因子 (示例)")
print(f"3. 非交易日残留 (节假日, 忽略): {len(real_hd)} 天")