#!/bin/bash
# 三层因子评估: 统计显著性 → 边际贡献 → 步进回测
# 参数来源: config.yaml factor.evaluation (单一真相源)
# Grinold & Kahn (1999) + Harvey, Liu & Zhu (2016)
# 详见 docs/adr/007-factor-evaluation-standard.md
set -e
cd "$(dirname "$0")/.."

echo "============================================"
echo "Layer 1+2: 统计显著性 + 边际贡献 (t ≥ 2.0)"
echo "============================================"
echo "(运行 compute_factor_stats, ~180s)"

.venv/bin/python3 << 'PYEOF'
import sys, os, sqlite3, json, time
sys.path.insert(0, os.getcwd())
import numpy as np

# 1. 运行因子统计评估
print("Computing factor stats...", flush=True)
from factor.stats_cache import compute_factor_stats
stats = compute_factor_stats(n_symbols=${N_SYMBOLS:-800}, lookback=${LOOKBACK:-120})

factor_names = stats["factor_keys"]
ic_means = {name: ic for name, ic in zip(factor_names, stats["ic"])}
ic_irs = {name: ir for name, ir in zip(factor_names, stats["ic_ir"])}
corr = np.array(stats["corr"])

print(f"\n{len(factor_names)} factors evaluated")
print(f"IC range: {min(ic_means.values()):+.4f} ~ {max(ic_means.values()):+.4f}")

# 2. 边际贡献评估
from factor.marginal import compute_marginal_evaluation, rank_candidates

results = compute_marginal_evaluation(
    factor_names, ic_means, ic_irs, corr, n_days=${N_DAYS:-120}, t_threshold=2.0
)

# 3. 输出结果
print(f"\n=== Layer 1: IC t-test (t ≥ 2.0) ===")
passed_t = 0
for name in sorted(factor_names, key=lambda n: abs(results[n]['t_stat']), reverse=True):
    r = results[name]
    tag = "✓" if r["t_pass"] else "✗"
    print(f"  {tag} {name:30s} IC={r['ic']:+.4f}  t={r['t_stat']:.1f}  IR={ic_irs.get(name,0):+.2f}")
    if r["t_pass"]:
        passed_t += 1
print(f"  → {passed_t}/{len(factor_names)} passed t-test")

print(f"\n=== Layer 2: Marginal IC (边际贡献) ===")
ranked = rank_candidates(results)
for i, (name, mic, r) in enumerate(ranked):
    marg_tag = "✓" if r.get("passes") else "✗"
    mic_str = f"{mic:+.4f}" if mic is not None else "N/A"
    print(f"  {i+1:2d}. {marg_tag} {name:30s} marginal_IC={mic_str}  ({r['reason']})")

# 4. 筛选: t-test 通过 + 边际贡献正向
candidates = [
    (name, mic, r) for name, mic, r in ranked
    if r.get("t_pass") and (mic is not None and mic > 0)
]

print(f"\n=== Candidates for stepwise backtest ({len(candidates)}) ===")
for i, (name, mic, r) in enumerate(candidates):
    print(f"  {i+1}. {name:30s} marginal_IC={mic:+.4f}")

# 5. 保存到临时文件
with open('/tmp/_eval_candidates.json', 'w') as f:
    json.dump({
        'candidates': [(n, float(m)) for n, m, _ in candidates],
        'ic_means': {k: float(v) for k, v in ic_means.items()},
        'ic_irs': {k: float(v) for k, v in ic_irs.items()},
        'results': {k: {kk: float(vv) if isinstance(vv, (int, float, np.floating)) else vv
                       for kk, vv in v.items()}
                    for k, v in results.items()},
    }, f, indent=2)
print(f"\nCandidates saved to /tmp/_eval_candidates.json")

# 6. 没有候选因子的兜底处理
if not candidates:
    print("\nWARNING: No candidates passed both t-test and marginal IC. Using t-test-only fallback.")
    candidates = [(name, 0.0, r) for name, mic, r in ranked if r.get("t_pass")]
    with open('/tmp/_eval_candidates.json', 'w') as f:
        json.dump({
            'candidates': [(n, float(m)) for n, m, _ in candidates],
            'ic_means': ic_means,
            'ic_irs': ic_irs,
            'results': {},
        }, f, indent=2)
PYEOF

echo ""
echo "============================================"
echo "Layer 3: 步进回测 (边际贡献排序)"
echo "============================================"

.venv/bin/python3 << 'PYEOF'
import json, sqlite3, subprocess, sys, os, re, time, numpy as np

sys.path.insert(0, '.')

with open('/tmp/_eval_candidates.json') as f:
    data = json.load(f)

candidates = data['candidates']
if not candidates:
    print("No candidates to test.")
    sys.exit(1)

print(f"Testing {len(candidates)} candidates in stepwise order:")
for i, (name, mic) in enumerate(candidates):
    print(f"  {i+1}. {name:25s} marginal_IC={mic:+.4f}")

# 从单因子开始, 逐步添加
conn = sqlite3.connect('data/market.db')

# 先全部失活, 再逐个激活测试
conn.execute("UPDATE factor_registry SET status='deprecated', status_reason='stepwise evaluation', updated_at=datetime('now','localtime')")
conn.commit()

kept = []
best_ir = -999

for idx, (name, marginal_ic) in enumerate(candidates):
    test_set = kept + [name]

    # 激活测试集
    conn.execute("UPDATE factor_registry SET status='deprecated'")
    for n in test_set:
        conn.execute("UPDATE factor_registry SET status='active', status_reason=NULL WHERE name=?", (n,))
    conn.commit()

    print(f"\n[{idx+1}/{len(candidates)}] Test: {', '.join(test_set)}", flush=True)

    tt = time.time()
    r = subprocess.run([
        '.venv/bin/python3', '-c',
        f"""
import sys; sys.path.insert(0, '.')
from backtest import run_backtest
result = run_backtest('2026-01-01', '2026-06-30', 5000)

# Compute IR (Information Ratio)
ret = result['total_wealth'].pct_change().dropna()
bench_ret = ret * 0  # simplified: benchmark returns ~0 for short periods
excess = ret - bench_ret
ir = float(excess.mean() / excess.std() * (252**0.5)) if len(excess) > 1 and excess.std() > 0 else 0.0

wealth = result['total_wealth'].iloc[-1]
daily_ret = result['total_wealth'].pct_change().dropna()
sharpe = float(daily_ret.mean() / daily_ret.std() * (252**0.5)) if len(daily_ret) > 1 and daily_ret.std() > 0 else 0.0

print(f'WEALTH={{wealth:.2f}}')
print(f'SHARPE={{sharpe:.4f}}')
print(f'IR={{ir:.4f}}')
"""
    ], capture_output=True, text=True, timeout=300, env={**os.environ, 'PYTHONPATH': '.'})

    elapsed = time.time() - tt

    wm = re.search(r'WEALTH=([\d.-]+)', r.stdout)
    sm = re.search(r'SHARPE=([\d.-]+)', r.stdout)
    im = re.search(r'IR=([\d.-]+)', r.stdout)

    w = float(wm.group(1)) if wm else 0
    s = float(sm.group(1)) if sm else 0
    ir = float(im.group(1)) if im else 0

    prev_ir = best_ir
    if ir >= best_ir:
        action = "KEEP"
        kept.append(name)
        best_ir = ir
    else:
        action = "DROP"

    print(f"  Wealth=¥{w:.2f}  Sharpe={s:.4f}  IR={ir:.4f}  (prev_IR={prev_ir:.4f})  → {action}  ({elapsed:.0f}s)")

    if action == "DROP":
        # 回退
        conn.execute("UPDATE factor_registry SET status='deprecated'")
        for n in kept:
            conn.execute("UPDATE factor_registry SET status='active', status_reason='passed stepwise backtest' WHERE name=?", (n,))
        conn.commit()

# 最终结果
conn.execute("UPDATE factor_registry SET status='deprecated'")
for name in kept:
    conn.execute(
        "UPDATE factor_registry SET status='active', status_reason='passed stepwise backtest', updated_at=datetime('now','localtime') WHERE name=?",
        (name,)
    )
conn.commit()

print(f"\n=== FINAL: {len(kept)} factors ===")
for name in kept:
    r = conn.execute("SELECT ic_mean, ic_ir FROM factor_registry WHERE name=?", (name,)).fetchone()
    print(f"  {name:30s} IC={r[0]:+.4f}  IR={r[1]:+.2f}")

conn.close()
print(f"\nStepwise evaluation complete.")
PYEOF

echo ""
echo "============================================"
echo "最终回测"
echo "============================================"
bash scripts/backtest_jq.sh
eval $(.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from config.loader import get as _ecfg
print(f'N_SYMBOLS={_ecfg(\"factor.evaluation.n_symbols\", 800)}')
print(f'LOOKBACK={_ecfg(\"factor.evaluation.lookback\", 120)}')
print(f'N_DAYS={_ecfg(\"factor.evaluation.n_days\", 120)}')
")
