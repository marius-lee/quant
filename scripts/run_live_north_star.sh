#!/bin/bash
# === North Star Live Trading — Nano tier, 3 factors, equal_weight post-warmup ===
# v644: ¥5,000 → ¥104,420 (20.9x) at 1,888.5% CAGR (backtest confirmed)
#
# 生产环境每日信号生成:
# 1. IC 每 5 天重新训练 (retrain_freq=5) — warmup 使用 ic_weighted
# 2. post-warmup 使用 equal_weight — 避免 Q3 2026 动量衰退
# 3. Universe=500, LOT_SIZE=100 (A 股标准)
# 4. Nano tier: _rank_concentrated, 2-3 只股票, 85% 资金利用率
#
# 用法:
#   PYTHONPATH=. bash scripts/run_live_north_star.sh

set -euo pipefail

cd "$(dirname "$0")/.."

echo "=============================================="
echo "North Star v644 — Daily Signal Generation"
echo "  Capital: ¥5,000 (Nano tier)"
echo "  Factors: alpha_momentum_20d, alpha_rsi_14d, alpha002_vol_div"
echo "  Combine: ic_weighted warmup → equal_weight post-warmup"
echo "  Universe: 500 | LOT_SIZE: 100 | retrain_freq: 5"
echo "=============================================="

PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.pipeline.execute import run_daily_pipeline
    run_daily_pipeline(
        strategy='nano_north_star_v644',
        combine_mode='equal_weight',
        universe_size=500,
        retrain_freq=5,
    )
    print('✅ Signal generation complete')
"
