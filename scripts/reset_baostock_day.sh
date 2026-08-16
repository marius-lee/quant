#!/usr/bin/env bash
# 手动重置 baostock 日请求计数 + 解除黑名单冷却 (v513 兜底)
#
# 背景: v513 起换热点 (公网 IP 变化) 由系统自动检测 — task_scope 进入时探测 IP,
#       发现变化即清零日计数 + 清除 blacklisted_at, 无需人工干预.
#       本脚本仅作兜底: IP 探测服务不可用 / 探测失败降级时, 手动执行清零.
#
# 用法:
#   bash scripts/reset_baostock_day.sh     # 确认已换网络后执行, 幂等
#
# 说明: 只清日计数与黑名单标记, 不动任务互斥锁 (.baostock_task.busy)。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python -c "
from quant.utils.baostock_gate import gate
st = gate.reset_day()
print('日计数已重置:', st.get('day'), 'count=', st.get('day_count', 0))
print('blacklisted_at 已清除' if 'blacklisted_at' not in st else 'WARN: blacklisted_at 仍存在')
"
echo "✔ 完成 — 可重新运行 bash scripts/resume_industry_sync.sh 续跑"