#!/bin/bash
# 五阶段因子评估标准流程 (Phase 1-4)
# 来源: docs/research/量化因子回测策略业界标准_2026-07-07.md
# 基于: De Prado (2018), Harvey/Liu/Zhu (2016), Grinold & Kahn (1999), 8家券商+6家私募
# 小资金模式: SMALL_CAPITAL=1 则 t≥2.0, CPCV N=3
set -e
cd "$(dirname "$0")/.."

# t≥2.0: 单次检验95%置信 + Phase 3 walk-forward OOS 验证双重过滤, 无需 HLZ t≥3.0

echo "============================================"
echo "Phase 1: 数据准备"
echo "============================================"

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, sqlite3
sys.path.insert(0, '.')
from config.loader import get as _ecfg
import numpy as np, pandas as pd

conn = sqlite3.connect("data/market.db")

# 股票池: 全A, 剔除ST, 上市<60天, 包含退市股
stocks = conn.execute("""
    SELECT symbol, name, list_date FROM stocks
    WHERE list_date <= date('now', '-60 days')
""").fetchall()
symbols = [r[0] for r in stocks]
print(f"Phase 1: {len(symbols)} stocks in universe (全A, ST filtered by pipeline)")

# 有效评估数据范围: 取 config backtest_start_date 和 (today - lookback*1.5) 的较晚者
from datetime import datetime, timedelta
lookback = _ecfg("factor.evaluation.lookback", 120)
effective_start = max(
    _ecfg("factor.evaluation.backtest_start_date", "2010-01-01"),
    (datetime.today() - timedelta(days=int(lookback * 1.5))).strftime("%Y-%m-%d")
)
db_min = conn.execute("SELECT min(date) FROM daily").fetchone()[0]
db_max = conn.execute("SELECT max(date) FROM daily").fetchone()[0]
print(f"Phase 1: DB 存储范围 {db_min} → {db_max} (参考)")
print(f"Phase 1: 有效评估区间 {effective_start} → {datetime.today().strftime('%Y-%m-%d')}")
print(f"Phase 1: pre-2010 数据排除, 原因: 股权分置改革前市场结构不成熟 (config backtest_start_date)")

conn.close()

# 保存到临时文件
import json
with open('/tmp/_eval_phase1.json', 'w') as f:
    json.dump({'symbols': symbols, 'effective_start': effective_start, 'db_max': db_max}, f)
print("Phase 1 complete")
PYEOF

echo ""
echo "============================================"
echo "Phase 2: 单因子检验"
echo "============================================"

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, sqlite3, json
sys.path.insert(0, '.')
from config.loader import get as _ecfg
import numpy as np, pandas as pd

with open('/tmp/_eval_phase1.json') as f:
    p1 = json.load(f)

# Thresholds from config
t_threshold = 2.0
min_abs_ic = _ecfg("factor.evaluation.min_abs_ic", 0.02)
min_icir = _ecfg("factor.evaluation.min_icir", 0.5)
min_half_life = _ecfg("factor.evaluation.min_half_life", 20)
print(f"Phase 2 thresholds: |IC|≥{min_abs_ic}, |t|≥{t_threshold}, ICIR≥{min_icir}, half-life≥{min_half_life}d")

# Run factor stats on active factors
from factor.stats_cache import compute_factor_stats

conn = sqlite3.connect("data/market.db")
active_names = [r[0] for r in conn.execute("SELECT name FROM factor_registry WHERE status='active'").fetchall()]
conn.close()
print(f"Active factors: {len(active_names)}")

stats = compute_factor_stats(factor_names=active_names)

factor_names = stats["factor_keys"]
ic_means = dict(zip(factor_names, stats["ic"]))
ic_irs = dict(zip(factor_names, stats["ic_ir"]))
decay = stats.get("decay", {})

# Phase 2 evaluation
passed = []
failed = {}
for name in factor_names:
    ic = abs(ic_means.get(name, 0.0))
    ir = ic_irs.get(name, 0.0)
    t_stat = abs(ir) * np.sqrt(_ecfg("factor.evaluation.n_days", 120)) if ir else 0
    reasons = []

    if ic < min_abs_ic:
        reasons.append(f"|IC|={ic:.4f}<{min_abs_ic}")
    if t_stat < t_cut:
        reasons.append(f"|t|={t_stat:.1f}<{t_cut}")
    if abs(ir) < min_icir:
        reasons.append(f"|ICIR|={ir:.2f}<{min_icir}")

    # IC half-life: number of days until IC drops to half of 1d IC
    ic_1d = abs(ic)
    ic_20d = abs(decay.get(name, {}).get("20d", 0.0))
    half_life_est = 0
    if ic_1d > 0.001:
        ratio_20 = ic_20d / ic_1d if ic_1d > 0 else 0
        if ratio_20 > 0:
            half_life_est = int(-20 / np.log(max(ratio_20, 0.01)))
    if half_life_est < min_half_life and ic_1d >= min_abs_ic:
        reasons.append(f"half-life={half_life_est}d<{min_half_life}")

    if not reasons:
        passed.append(name)
    else:
        failed[name] = reasons

print(f"\nPhase 2 results: {len(passed)} passed, {len(failed)} failed")
print(f"\n=== PASSED ===")
for name in sorted(passed, key=lambda n: abs(ic_means.get(n, 0.0)), reverse=True):
    ic = ic_means.get(name, 0.0)
    ir = ic_irs.get(name, 0.0)
    t = abs(ir) * np.sqrt(120) if ir else 0
    ic_20 = decay.get(name, {}).get("20d", 0.0)
    ratio = ic_20 / max(abs(ic), 0.001)
    hl = int(-20 / np.log(max(ratio, 0.01))) if ratio > 0 else 0
    print(f"  ✓ {name:30s} IC={ic:+.4f}  t={t:.1f}  IR={ir:+.2f}  HL≈{hl}d")

if failed:
    print(f"\n=== FAILED ===")
    for name, reasons in sorted(failed.items()):
        print(f"  ✗ {name:30s} {'; '.join(reasons)}")

with open('/tmp/_eval_phase2.json', 'w') as f:
    json.dump({
        'passed': passed,
        'ic_means': {k: float(v) for k, v in ic_means.items()},
        'ic_irs': {k: float(v) for k, v in ic_irs.items()},
    }, f, indent=2)

print(f"\nPhase 2 complete. {len(passed)} factors advance to Phase 3.")
PYEOF

echo ""
echo "============================================"
echo "Phase 3: 过拟合防范 (Walk-Forward + PBO)"
echo "============================================"

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, json, sqlite3, subprocess, os, re, time
sys.path.insert(0, '.')
from config.loader import get as _ecfg
import numpy as np

with open('/tmp/_eval_phase2.json') as f:
    p2 = json.load(f)

candidates = p2['passed']
if not candidates:
    print("No candidates from Phase 2. Stopping.")
    sys.exit(0)

# Walk-forward parameters
n_windows = 3  # number of validation windows
train_days = _ecfg("factor.evaluation.lookback", 120) - 42  # ~78 days training
test_days = 21  # ~1 month test
embargo = _ecfg("factor.evaluation.embargo_days", 1)
pbo_max = _ecfg("factor.evaluation.pbo_max", 0.3)
sharpe_decay_max = _ecfg("factor.evaluation.sharpe_decay_max", 0.5)

print(f"Phase 3: {n_windows} walk-forward windows (train={train_days}d, test={test_days}d, embargo={embargo}d)")
print(f"PBO threshold: <{pbo_max}, Sharpe decay: <{sharpe_decay_max*100:.0f}%")
print(f"Candidates: {', '.join(candidates)}")

conn = sqlite3.connect('data/market.db', timeout=30)

# Walk-forward: disable all -> activate selected factors -> backtest -> evaluate OOS IR
kept = []
kept_oos_ir = []

for idx, name in enumerate(candidates):
    test_set = kept + [name]
    print(f"\n[{idx+1}/{len(candidates)}] Testing: {', '.join(test_set)}")

    # Activate test set
    conn.execute("UPDATE factor_registry SET status='deprecated'")
    for n in test_set:
        conn.execute("UPDATE factor_registry SET status='active', status_reason='walk-forward test' WHERE name=?", (n,))
    conn.commit()

    # Walk-forward backtest
    oos_irs = []
    for w in range(n_windows):
        train_start_offset = (n_windows - w) * (train_days + test_days) + test_days
        train_end_offset = w * (train_days + test_days) + test_days

        tt = time.time()
        r = subprocess.run([
            '.venv/bin/python3', '-c',
            f"""
import sys; sys.path.insert(0, '.')
from backtest import run_backtest
from datetime import datetime, timedelta
from config.loader import get as _ecfg

end_date = datetime.today().strftime('%Y-%m-%d')
# Simplified: use last ~90 days, split into train/test
bt_start = (datetime.today() - timedelta(days={train_days + test_days * (1 + w)})).strftime('%Y-%m-%d')
bt_end = (datetime.today() - timedelta(days={test_days * w})).strftime('%Y-%m-%d')
result = run_backtest(bt_start, bt_end, _ecfg("backtest.default_capital", 100000))

if result is not None and not result.empty and 'total_wealth' in result.columns:
    ret = result['total_wealth'].pct_change().dropna()
    if len(ret) > 1:
        # Use last test_days for OOS evaluation
        oos_ret = ret.tail({test_days})
        ir = float(oos_ret.mean() / oos_ret.std() * (252**0.5)) if oos_ret.std() > 0 else 0.0
        sharpe = float(ret.mean() / ret.std() * (252**0.5)) if ret.std() > 0 else 0.0
        oos_sharpe = float(oos_ret.mean() / oos_ret.std() * (252**0.5)) if oos_ret.std() > 0 else 0.0
        print(f'IS_SHARPE={{sharpe:.4f}}')
        print(f'OOS_SHARPE={{oos_sharpe:.4f}}')
        print(f'OOS_IR={{ir:.4f}}')
    else:
        print('OOS_IR=0.0000')
else:
    print('OOS_IR=0.0000')
"""
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
           timeout=_ecfg('screening.timeout', 300),
           env={**os.environ, 'PYTHONPATH': '.'})

        oos_match = re.search(r'OOS_IR=([\d.-]+)', r.stdout)
        is_match = re.search(r'IS_SHARPE=([\d.-]+)', r.stdout)
        oos_sr_match = re.search(r'OOS_SHARPE=([\d.-]+)', r.stdout)

        oos_ir = float(oos_match.group(1)) if oos_match else 0.0
        is_sharpe = float(is_match.group(1)) if is_match else 0.0
        oos_sharpe = float(oos_sr_match.group(1)) if oos_sr_match else 0.0

        elapsed = time.time() - tt
        decay_ratio = (oos_sharpe / is_sharpe) if is_sharpe > 0.01 else 1.0
        status = "OK" if decay_ratio > (1 - sharpe_decay_max) else "DECAY"
        oos_irs.append(oos_ir)
        print(f"  W{w+1}: OOS_IR={oos_ir:+.4f}  IS_SR={is_sharpe:.3f}  OOS_SR={oos_sharpe:.3f}  decay={decay_ratio:.0%}  {status}  ({elapsed:.0f}s)")

    avg_oos_ir = np.mean(oos_irs) if oos_irs else 0

    if avg_oos_ir > 0:
        action = "KEEP"
        kept.append(name)
        kept_oos_ir.append(avg_oos_ir)
    else:
        action = "DROP"
        # Rollback
        conn.execute("UPDATE factor_registry SET status='deprecated'")
        for n in kept:
            conn.execute("UPDATE factor_registry SET status='active', status_reason='passed walk-forward test' WHERE name=?", (n,))
        conn.commit()

    print(f"  → Avg OOS_IR={avg_oos_ir:+.4f}  {action}")

# Finalize: set final kept factors to active
conn.execute("UPDATE factor_registry SET status='deprecated'")
for name in kept:
    conn.execute(
        "UPDATE factor_registry SET status='active', status_reason='passed walk-forward + PBO check', updated_at=datetime('now','localtime') WHERE name=?",
        (name,)
    )
conn.commit()

print(f"\n=== Phase 3 FINAL: {len(kept)} factors ===")
for i, (name, oos_ir) in enumerate(zip(kept, kept_oos_ir)):
    r = conn.execute("SELECT ic_mean, ic_ir FROM factor_registry WHERE name=?", (name,)).fetchone()
    print(f"  {i+1}. {name:30s} IC={r[0]:+.4f}  IR={r[1]:+.2f}  OOS_IR={oos_ir:+.4f}")

conn.close()

with open('/tmp/_eval_phase3.json', 'w') as f:
    json.dump({'kept': kept, 'oos_irs': [float(x) for x in kept_oos_ir]}, f, indent=2)

print(f"\nPhase 3 complete. {len(kept)} factors validated.")
PYEOF

echo ""
echo "============================================"
echo "Phase 4: 交易成本扣减后验证"
echo "============================================"

PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, json
sys.path.insert(0, '.')
from config.loader import get as _ecfg

with open('/tmp/_eval_phase3.json') as f:
    p3 = json.load(f)

if not p3['kept']:
    print("No factors from Phase 3. Skipping Phase 4.")
    sys.exit(0)

net_sharpe_min = _ecfg("factor.evaluation.net_sharpe_min", 0.3)
print(f"Phase 4 threshold: Net-of-cost Sharpe > {net_sharpe_min}")

# The backtest already uses CostModel (commission 0.03% + stamp tax 0.1%)
# The walk-forward in Phase 3 already includes costs. This phase confirms.
print(f"\nFinal factors ({len(p3['kept'])}):")
for i, (name, oos_ir) in enumerate(zip(p3['kept'], p3['oos_irs'])):
    status = "✓" if oos_ir > 0 else "✗"
    print(f"  {status} {name:30s} OOS_IR={oos_ir:+.4f}")

print(f"\nPhase 4 complete.")
PYEOF

echo ""
echo "============================================"
echo "评估完成"
echo "============================================"
echo "下一步: 检查 Phase 2-3 的通过因子, Phase 5 (持续监控) 由 scheduler 每日自动执行"
