"""一键 OOS + 拥挤度验证."""
from quant.scheduler.oos_verify import run_oos_check
from quant.scheduler.crowdedness import check_factor_crowdedness

print("=== G1 OOS Walk-Forward ===")
result = run_oos_check("2026-07-15")
print(f"n_factors={result['n_factors']}  n_qualified={result['n_qualified']}  "
      f"IS_IR={result['is_ir']:+.4f}  OOS_IR={result['oos_ir']:+.4f}  "
      f"decay={result['decay_ratio']:.2%}")
if result.get('alert'):
    print(f"⚠  OOS decay alert: {result['oos_decay_count']} factors decayed")
for f, d in result['details'].get('per_factor', {}).items():
    print(f"  {f}: IS_IR={d['is_ir']:+.4f} → OOS_IR={d['oos_ir']:+.4f}  "
          f"IS={d['n_is']}d OOS={d['n_oos']}d  "
          f"IS_mean={d['is_mean']:+.4f} OOS_mean={d['oos_mean']:+.4f}")

print()
print("=== G2 Crowdedness ===")
crowd = check_factor_crowdedness("2026-07-15")
print(f"factors={crowd['n_factors']}  crowd_index={crowd['crowd_index']:.3f}  "
      f"high_corr_pairs={crowd['n_high_corr_pairs']}  alert={crowd['alert']}")
if crowd['high_corr_pairs']:
    for p in crowd['high_corr_pairs']:
        print(f"  {p['factor_a']} ↔ {p['factor_b']}: ρ={p['correlation']:+.3f}")
