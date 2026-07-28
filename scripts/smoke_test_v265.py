"""Smoke test for test-v265 — IC scope isolation + fund_flow resilience."""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QUANT_ENV', 'dev')

PASS, FAIL = 0, 0

def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  OK  {name}")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL  {name}: {e}")

# ── 1. oos_verify accepts status_filter ──
def t1():
    from quant.scheduler.oos_verify import run_oos_check
    import inspect
    sig = inspect.signature(run_oos_check)
    assert 'status_filter' in sig.parameters, "status_filter not in signature"

# ── 2. factor_ic_daily has scope column ──
def t2():
    from quant.data.repos.factor_repo import FactorRepo
    f = FactorRepo()
    f.ensure_ic_daily_table()
    conn = f._conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(factor_ic_daily)").fetchall()}
    assert 'scope' in cols, "scope column missing"

# ── 3. insert_ic_daily accepts scope ──
def t3():
    from quant.data.repos.factor_repo import FactorRepo
    import inspect
    sig = inspect.signature(FactorRepo.insert_ic_daily)
    assert 'scope' in sig.parameters, "scope param missing from insert_ic_daily"

# ── 4. get_ic_rolling accepts scope ──
def t4():
    from quant.data.repos.factor_repo import FactorRepo
    import inspect
    sig = inspect.signature(FactorRepo.get_ic_rolling)
    assert 'scope' in sig.parameters, "scope param missing from get_ic_rolling"

# ── 5. compute_backtest_ic exists ──
def t5():
    from quant.factor.stats_cache import compute_backtest_ic
    assert callable(compute_backtest_ic), "compute_backtest_ic not callable"

# ── 6. load_ic_map_from_cache accepts scope ──
def t6():
    from quant.factor.stats_cache import load_ic_map_from_cache
    import inspect
    sig = inspect.signature(load_ic_map_from_cache)
    assert 'scope' in sig.parameters, "scope param missing from load_ic_map_from_cache"

# ── 7. generate_signals has scope param ──
def t7():
    from quant.pipeline import generate_signals
    import inspect
    sig = inspect.signature(generate_signals)
    assert 'scope' in sig.parameters, "scope param missing from generate_signals"

# ── 8. Backtest uses compute_backtest_ic (not _compute_ic) ──
def t8():
    content = open('quant/backtest/loop.py').read()
    assert 'compute_backtest_ic' in content, "backtest/loop.py missing compute_backtest_ic"
    assert 'from quant.factor.stats_cache import compute_backtest_ic' in content, "wrong import"

# ── 9. fund_flow has BaseException handler ──
def t9():
    content = open('quant/data/fund_flow.py').read()
    assert 'except BaseException as e:' in content, "fund_flow missing BaseException handler"

# ── 10. Imports ──
def t10():
    from quant.scheduler.attribution import _run
    from quant.backtest.loop import run_backtest
    from quant.data.fund_flow import sync_all

print("=" * 50)
check("oos_verify status_filter param", t1)
check("factor_ic_daily scope column", t2)
check("insert_ic_daily scope param", t3)
check("get_ic_rolling scope param", t4)
check("compute_backtest_ic exists", t5)
check("load_ic_map_from_cache scope param", t6)
check("generate_signals scope param", t7)
check("backtest uses compute_backtest_ic", t8)
check("fund_flow BaseException handler", t9)
check("Imports", t10)
print("=" * 50)
print(f"{'ALL PASSED' if FAIL == 0 else f'{FAIL} FAILED'}  ({PASS}/{PASS+FAIL})")
sys.exit(0 if FAIL == 0 else 1)
