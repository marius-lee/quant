#!/usr/bin/env bash
# 行业 PIT 历史同步 (v502) — baostock updateDate 回跳法逐股重建 industry_history 表.
#
# 用法:
#   bash scripts/sync_industry_history.sh            # 全量 (幂等, 断点续跑, 约 1-2h)
#   bash scripts/sync_industry_history.sh 100        # 只同步前 100 只未完成股票 (试跑)
#   PYTHONPART=... bash scripts/sync_industry_history.sh
#
# 幂等: industry_history 已有符号跳过; 已建数据不重复拉取.
set -euo pipefail
cd "$(dirname "$0")/.."

BATCH="${1:-}"

if [ -n "$BATCH" ]; then
  PYTHONPATH=. .venv/bin/python -c "
from quant.data.industry_history import sync_history
print(sync_history(batch=$BATCH))
"
else
  PYTHONPATH=. .venv/bin/python -c "
from quant.data.industry_history import sync_history
print(sync_history())
"
fi