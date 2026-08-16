#!/usr/bin/env bash
# 行业 PIT 同步完整性校验 + 回测浅跑冒烟 (v502).
#
# 用途: industry_history 后台同步完成后, 验证:
#   1. 表覆盖全市场 (5557 只) 且无异常缺段
#   2. 回测能跑通 (loop.py industry pivot dtype 修复生效, 中性化不崩)
# 用法: bash scripts/verify_industry_pit.sh
# 幂等: 只读校验 + 40 天 smoke 回测, 可重复执行. 同期需 < 全量对拍 (run_backtests.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 - <<'EOF'
import sqlite3
from quant.config.paths import MARKET_DB

conn = sqlite3.connect(MARKET_DB)
total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
synced = conn.execute("SELECT COUNT(DISTINCT symbol) FROM industry_history").fetchone()[0]
segments = conn.execute("SELECT COUNT(*) FROM industry_history").fetchone()[0]
skipped = conn.execute("SELECT COUNT(*) FROM industry_history_skip").fetchone()[0]

print(f"industry_history: {synced}/{total} symbols ({skipped} skipped), {segments} segments")
if synced + skipped < total:
    raise SystemExit(f"ABORT: 同步未完成 ({synced}+{skipped}/{total}) — 请先等后台同步跑完再验证")
zero_seg = conn.execute(
    "SELECT COUNT(*) FROM stocks s LEFT JOIN industry_history h ON s.symbol=h.symbol "
    "LEFT JOIN industry_history_skip k ON s.symbol=k.symbol "
    "WHERE h.symbol IS NULL AND k.symbol IS NULL").fetchone()[0]
print(f"零段且未标记跳过股票: {zero_seg}")
conn.close()
EOF

echo ""
echo "=== smoke 回测 (40 天) — 验证 PIT pivot + 中性化不崩 ==="
PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2026-06-01', '2026-07-27', capital=5000, mode='smoke')
m = r['metrics']
print(f\"Sharpe={m['sharpe']}  CAGR={m['cagr_pct']}%  MDD={m['max_drawdown_pct']}%\")
print(f\"equity=¥{m['final_equity']:,.0f}  days={m['n_days']}  errors={r['errors']}  elapsed={r['elapsed_sec']}s\")
assert r['errors'] == 0, 'smoke 回测有错误'
print('PASS: industry PIT smoke 回测通过')
"