#!/bin/bash
# test-v306 夜间回填链 (2026-07-26):
#   ② fund_flow 全量 (东财解封探测, 每 15min, 截止 23:30)
#   ① jq_valuation 2026-07-06..07-24 (tushare 日配额 00:00 重置后执行)
#   ④ factor_cache 增量物化 → ⑤ verify_v305 终验
# 日志: logs/v306_overnight.log (主), 各步骤单独日志 logs/v306_<step>.log
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
exec >>logs/v306_overnight.log 2>&1

log() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

probe_em() {
    curl -sS -m 15 \
        -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
        -H "Referer: https://data.eastmoney.com/" \
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=5&klt=101&secid=1.600519&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&ut=b2884a393a59ad64002292a3e90d46a5" \
        2>/dev/null | head -c 200 | grep -q '"rc":0'
}

log "=== v306 overnight chain start ==="

# ── 等待其它写库进程退出 (最多 10min) ──
for _ in $(seq 1 20); do
    if ! lsof -t quant/data/market.db >/dev/null 2>&1; then break; fi
    log "market.db busy, wait 30s"
    sleep 30
done

# ── ② fund_flow: 东财解封探测 (19:xx 被封, 冷却重试) ──
ff_done=0
probe_deadline=$(date -v23H -v30M +%s)
while [ "$(date +%s)" -lt "$probe_deadline" ]; do
    if probe_em; then
        log "eastmoney probe OK, start fund_flow full sync"
        PYTHONPATH=. .venv/bin/python -u quant/data/fund_flow.py 0 \
            >>logs/v306_fund_flow.log 2>&1
        log "fund_flow exited rc=$?"
        ff_done=1
        break
    fi
    log "eastmoney still blocked, retry in 15min"
    sleep 900
done
[ "$ff_done" = 0 ] && log "WARN: eastmoney blocked past 23:30, fund_flow skipped tonight"

# ── ① jq_valuation: 等到 tushare 日配额重置 (次日 00:05) ──
sleep_secs=$(.venv/bin/python -c "
from datetime import datetime, timedelta
now = datetime.now()
t = now.replace(hour=0, minute=5, second=0, microsecond=0)
if t <= now:
    t += timedelta(days=1)
print(int((t - now).total_seconds()))")
log "sleep ${sleep_secs}s until 00:05 for tushare quota reset"
sleep "$sleep_secs"
log "start jq_valuation 2026-07-06..2026-07-24"
PYTHONPATH=. .venv/bin/python -u quant/data/jq_valuation.py 2026-07-06 2026-07-24 \
    >>logs/v306_jq_valuation.log 2>&1
log "jq_valuation exited rc=$?"

# ── ④ factor_cache 增量物化 ──
log "start factor_cache materialize to 2026-07-24"
bash scripts/run_task.sh factor_cache 2026-07-24 >>logs/v306_factor_cache.log 2>&1
log "factor_cache exited rc=$?"

# ── ⑤ verify_v305 终验 ──
log "=== verify_v305 ==="
PYTHONPATH=. .venv/bin/python scripts/verify_v305.py
log "=== v306 overnight chain done ==="
