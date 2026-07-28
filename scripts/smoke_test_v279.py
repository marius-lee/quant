"""Smoke test for test-v280 — end-to-end backtest (smoke mode: ~22 days × 10 stocks)."""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QUANT_ENV', 'dev')

_T0 = time.perf_counter()
PASS, FAIL = 0, 0

def check(label, fn):
    global PASS, FAIL
    t0 = time.perf_counter()
    try:
        fn()
        PASS += 1
        dt = time.perf_counter() - t0
        print(f"  OK  {label}  ({dt:.1f}s)")
    except Exception as e:
        FAIL += 1
        dt = time.perf_counter() - t0
        print(f"  FAIL  {label}: {e}  ({dt:.1f}s)")

print("=" * 60)

# ── 1. Import chain ──
def t1():
    from quant.factor.stats_cache import compute_backtest_ic, _bayesian_shrink_ic_map, _extract_float_weights
    from quant.backtest.analyze import diagnose, apply_diagnosis
    from quant.pipeline import generate_signals
    from quant.backtest.loop import run_backtest
    from quant.factor.store import FactorStore
check("Full import chain", t1)

# ── 2. 252→244 config ──
def t2():
    from quant.config.constants import _require_cfg
    assert _require_cfg("market.annual_trading_days") == 244, "annual_trading_days != 244"
    assert _require_cfg("backtest.smoke.universe_size") == 10, "smoke.universe_size != 10"
    from quant.config import loader as cfgl; assert cfgl.get('backtest.universe_size') is None, "full universe_size should be None (all)"
check("252→244 config + smoke/full dimensions", t2)

# ── 3. FactorStore → pipeline 数据流 (load one date) ──
def t3():
    from quant.factor.store import FactorStore
    from quant.config.paths import FACTOR_CACHE_DB
    fs = FactorStore(db_path=FACTOR_CACHE_DB)
    fv = fs.load('2026-07-22', factor_names=None)
    assert isinstance(fv, dict), f"expected dict, got {type(fv)}"
    assert len(fv) > 0, "empty factor_values from cache"
    n_syms = len(next(iter(fv.values())))
    print(f"   [{len(fv)} factors × {n_syms} symbols loaded]")
    fs.close()
check("FactorStore.load() from cache", t3)

# ── 4. Smoke backtest (22d × 10 stocks) ──
def t4():
    from quant.backtest.loop import run_backtest
    result = run_backtest(mode='smoke', capital=5000, ic_lookback=20)
    assert 'metrics' in result
    m = result['metrics']
    assert m['sharpe'] is not None
    assert m['n_days'] > 0
    print(f"   [{m['n_days']}d, Sharpe={m['sharpe']:.3f}, CAGR={m['cagr_pct']}%]")
check("Smoke backtest (22d × 10 stocks)", t4)

elapsed = time.perf_counter() - _T0
print("=" * 60)
print(f"{"ALL PASSED" if FAIL == 0 else f"{FAIL} FAILED"}  ({PASS}/{PASS+FAIL})  total={elapsed:.1f}s")
sys.exit(0 if FAIL == 0 else 1)
