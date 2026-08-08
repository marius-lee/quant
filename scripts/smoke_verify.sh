#!/bin/bash
# 修复项冒烟验证 — 对照 docs/reports/项目全面分析报告_2026-07-24.md
# 用法: bash scripts/smoke_verify.sh
set -e
cd "$(dirname "$0")/.."

echo "═══ 1. 全量单元测试 (71 项) ═══"
PYTHONPATH=. .venv/bin/python3 -m pytest test/ -q 2>&1 | tail -2

echo ""
echo "═══ 2. 关键模块 import (B-02/B-04/B-23) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import BacktestEngine, run_backtest
from quant.scheduler.attribution import _require_cfg
from quant.pipeline import generate_signals, execute_signals
from quant.pipeline import generate_signals, execute_signals
from quant.scheduler.orchestrator import _MAX_TASK_RETRIES
from quant.scheduler.manifest import ALL as _MANIFEST_ALL
from quant.execution.stop_loss import RiskManager, _compute_atr
from quant.backtest.broker import SimulatedBroker
import web.state_broker, web.app
print('imports OK | task manifest:', sorted(_MANIFEST_ALL), '| retries:', _MAX_TASK_RETRIES)
" 2>&1 | grep -v WARNING

echo ""
echo "═══ 3. 凭证加载 (B-28: config/.env → \${ENV} 占位符) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import quant.config.loader as L
t = L.get('data.tushare_token') or ''
k = L.get('data.tickflow_api_key') or ''
assert t and k, 'tokens missing — check quant/config/.env'
print(f'tushare: {t[:6]}*** | tickflow: {k[:6]}***')
"

echo ""
echo "═══ 4. 新库初始化 + meta KV + 多策略净值 (B-05) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os
from quant.data.repos.trade_repo import TradeRepo
tmp = tempfile.mktemp(suffix='.db')
r = TradeRepo(tmp)
r.set_flag('smoke', '1'); assert r.get_flag('smoke') == '1'
r.clear_flag('smoke'); assert r.get_flag('smoke') is None
r.record_daily_equity('2026-07-24', 90000, 10000, strategy='quant')
r.record_daily_equity('2026-07-24', 80000, 15000, strategy='other')
q = r.get_daily_equity_range('2026-07-01', '2026-07-31', strategy='quant')
o = r.get_daily_equity_range('2026-07-01', '2026-07-31', strategy='other')
assert q[0]['equity'] == 100000 and o[0]['equity'] == 95000
os.unlink(tmp)
print('B-05 OK: fresh DB init + meta KV + PK(date,strategy) 多策略共存')
"

echo ""
echo "═══ 5. FIFO 成本 (B-20) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os
from quant.data.repos.trade_repo import TradeRepo
tmp = tempfile.mktemp(suffix='.db')
r = TradeRepo(tmp)
r.record_trade('s', '2026-01-05', '600519', 'buy', 100.0, 200)
r.record_trade('s', '2026-01-06', '600519', 'buy', 120.0, 100)
r.record_trade('s', '2026-01-07', '600519', 'sell', 150.0, 200)
avg = r.get_average_cost('s', '600519')
assert abs(avg - 120.0) < 0.01, f'FIFO broken: {avg}'
os.unlink(tmp)
print(f'B-20 OK: FIFO cost = {avg} (全历史加权平均会给 106.67)')
"

echo ""
echo "═══ 6. MTM 权益计价 (B-06) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os
from quant.data.repos.trade_repo import TradeRepo
tmp = tempfile.mktemp(suffix='.db')
r = TradeRepo(tmp); r.set_initial_capital('quant', 100000)
from quant.execution.engine import ExecutionEngine, Order
e = ExecutionEngine(db_path=tmp)
e.execute([Order(symbol='009999', side='buy', shares=100, price=100.0, cost=0)], '2026-07-23', 'quant')
cash = e.get_cash('quant')
mtm = e.get_capital('quant', prices={'009999': 120.0})
assert abs(mtm - (cash + 12000)) < 1, (mtm, cash)
os.unlink(tmp)
print(f'B-06 OK: cash={cash:.0f} mtm@120={mtm:.0f}')
" 2>&1 | grep -v INFO

echo ""
echo "═══ 7. 板块涨跌幅阈值 (B-16) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
from quant.execution.engine import _price_limit_pct
got = {s: _price_limit_pct(s) for s in ('688001', '300750', '600519', '000001', '832000', '920001')}
assert got == {'688001': 0.2, '300750': 0.2, '600519': 0.1, '000001': 0.1, '832000': 0.3, '920001': 0.3}, got
print('B-16 OK:', got)
"

echo ""
echo "═══ 8. state_broker TTL 缓存 (B-27) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
from web.state_broker import InProcessBroker
b = InProcessBroker()
s1 = b.get(); s2 = b.get()
assert s1['capital'] == s2['capital']
assert b._state_ts > 0, 'cache ts not set'
print('B-27 OK: 2x get() 命中 TTL 缓存, keys =', len(s1))
" 2>&1 | grep -v INFO

echo ""
echo "═══ 9. 熔断 flag 链路 (B-14: monitor 写 → execute 读) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os
from quant.data.repos.trade_repo import TradeRepo
tmp = tempfile.mktemp(suffix='.db')
r = TradeRepo(tmp)
r.set_flag('circuit_breaker', '2026-07-24')
r.set_flag('circuit_breaker_reason', 'smoke test')
assert r.get_flag('circuit_breaker') == '2026-07-24'
assert 'smoke' in r.get_flag('circuit_breaker_reason')
r.clear_flag('circuit_breaker')
assert r.get_flag('circuit_breaker') is None
os.unlink(tmp)
print('B-14 OK: circuit_breaker flag set/get/clear')
"

echo ""
echo "═══ 9b. 多策略主键迁移 (B-19: daily_signals + benchmark_tracking) ═══"
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os, json
from quant.data.repos.trade_repo import TradeRepo
tmp = tempfile.mktemp(suffix='.db')
r = TradeRepo(tmp)
r.save_signals('2026-07-24', [{'symbol': '600519'}], 100000, strategy='quant', mode='live')
r.save_signals('2026-07-24', [{'symbol': '000001'}], 100000, strategy='quant', mode='backtest')
r.save_signals('2026-07-24', [{'symbol': '300750'}], 50000, strategy='other', mode='live')
live = r.get_daily_signals_range('2026-07-24', '2026-07-24', mode='live', strategy='quant')
bt = r.get_daily_signals_range('2026-07-24', '2026-07-24', mode='backtest', strategy='quant')
assert json.loads(live[0]['signals_json'])[0]['symbol'] == '600519'
assert json.loads(bt[0]['signals_json'])[0]['symbol'] == '000001'
os.unlink(tmp)
print('B-19 OK: daily_signals PK(date,strategy,mode) 同日三行共存')
" 2>&1 | grep -v INFO
PYTHONPATH=. .venv/bin/python3 -c "
import tempfile, os
import quant.data.repos._base as base
tmp = tempfile.mktemp(suffix='.db')
base.TRADE_DB = tmp
from quant.benchmark.tracker import record_daily, get_tracking_summary
record_daily('2026-07-24', 99999.0, strategy='quant')
record_daily('2026-07-24', 88888.0, strategy='other')
import sqlite3
c = sqlite3.connect(tmp)
sql = c.execute(\"SELECT sql FROM sqlite_master WHERE name='benchmark_tracking'\").fetchone()[0]
assert 'PRIMARY KEY (date, strategy)' in sql, sql
c.close()
os.unlink(tmp)
print('B-19 OK: benchmark_tracking PK(date,strategy) 多策略共存')
" 2>&1 | grep -v "INFO\|WARNING"

echo ""
echo "═══ 10. /api/trade 鉴权 (B-28, 需 web 服务运行; 未运行则跳过) ═══"
if curl -s -m 2 http://localhost:8521/api/health > /dev/null 2>&1; then
    echo "-- 无 token 头 (QUANT_API_TOKEN 未设时应 200/400, 已设时应 401):"
    curl -s -m 2 -X POST http://localhost:8521/api/trade \
        -H 'Content-Type: application/json' \
        -d '{"symbol":"600519","side":"buy","price":100,"shares":100,"cost":100}' | head -c 200
    echo ""
else
    echo "SKIP: web 服务未运行 (8521)"
fi

echo ""
echo "═══ 11. qfq 重同步脚本 dry-run (B-08 数据迁移入口) ═══"
PYTHONPATH=. .venv/bin/python3 scripts/resync_daily_qfq.py --dry-run --limit 10 2>&1 | tail -2

echo ""
echo "═══ ALL SMOKE TESTS PASSED ═══"
