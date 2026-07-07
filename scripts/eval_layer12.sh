#!/bin/bash
# Layer 1+2 快速评估: IC t-test (t from config) + 边际贡献
# 全量因子评估, 不修改 factor_registry status
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
import sys, sqlite3
sys.path.insert(0, '.')
from factor.stats_cache import compute_factor_stats
from config.loader import get as _ecfg
import numpy as np

conn = sqlite3.connect("data/market.db")
all_names = [r[0] for r in conn.execute("SELECT name FROM factor_registry WHERE status='active'").fetchall()]
conn.close()

print(f"Evaluating {len(all_names)} active factors (active factors, deprecated excluded)...")
stats = compute_factor_stats(factor_names=all_names)  # n_symbols/lookback from config.yaml

factor_names = stats["factor_keys"]
ic_means = dict(zip(factor_names, stats["ic"]))
ic_irs = dict(zip(factor_names, stats["ic_ir"]))
corr = np.array(stats["corr"])

from factor.marginal import compute_marginal_evaluation, rank_candidates
from config.loader import get as _ecfg
t_threshold = _ecfg('factor.evaluation.t_threshold_small', 2.0)
results = compute_marginal_evaluation(factor_names, ic_means, ic_irs, corr, n_days=120, t_threshold=t_threshold)

print(f"\n=== Layer 1: IC t-test (t >= 2.0) ===")
passed = 0
for name in sorted(factor_names, key=lambda n: abs(results[n]["t_stat"]), reverse=True):
    r = results[name]
    tag = "✓" if r["t_pass"] else "✗"
    print(f"  {tag} {name:30s} IC={r['ic']:+.4f}  t={r['t_stat']:.1f}  IR={ic_irs.get(name,0):+.2f}")
    if r["t_pass"]:
        passed += 1
print(f"  -> {passed}/{len(factor_names)} passed t-test")

print(f"\n=== Layer 2: Marginal IC (passing t-test only) ===")
for i, (name, mic, r) in enumerate(rank_candidates(results)):
    if r.get("t_pass"):
        print(f"  {name}: marginal_IC={mic:+.4f}  t={r['t_stat']:.1f}  {r['reason']}")
PYEOF
