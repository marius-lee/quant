"""test-v190 冒烟测试: 审计修复全链路验证"""
import numpy as np, pandas as pd
import sys, traceback

failed = 0

def check(name, fn):
    global failed
    try:
        fn()
        print(f"  ✅ {name}")
    except Exception as e:
        failed += 1
        print(f"  ❌ {name}: {e}")
        traceback.print_exc()

# ── C2: Ledoit-Wolf 收缩 ──
def t_c2():
    from quant.risk.covariance import ledoit_wolf_cov
    returns = pd.DataFrame(np.random.randn(60, 50) * 0.02, columns=[f"s{i}" for i in range(50)])
    cov = ledoit_wolf_cov(returns)
    assert cov.shape == (50, 50)
    assert not np.isnan(cov.values).any()
    S = returns.cov().values
    diff = np.abs(cov.values - S).mean()
    assert diff > 1e-6, f"shrinkage appears zero (diff={diff:.6f})"

# ── H6: 迭代裁剪 ──
def t_h6():
    from quant.optimizer.portfolio import _iterative_clip
    # Case 1: all weights > max_single → equal weight (correct fallback)
    w = _iterative_clip(np.array([0.1, 0.1, 0.8]), 0.05)
    assert abs(w.sum() - 1.0) < 0.001, f"sum != 1: {w.sum()}"
    assert (w == w[0]).all(), f"all-over should be equal weight, got {w}"
    # Case 2: some within limit → should be enforced
    w2 = _iterative_clip(np.array([0.04, 0.06, 0.9]), 0.05)
    assert (w2 <= 0.0501).all(), f"clip failed: {w2}"
    assert abs(w2.sum() - 1.0) < 0.001
    # Case 3: all within limit → no-op
    w3 = _iterative_clip(np.array([0.3, 0.3, 0.4]), 0.5)
    assert (w3 <= 0.5).all()

# ── H3: daily_equity 表 + 读写 ──
def t_h3():
    from quant.data.trade_repo import TradeRepo
    from datetime import date
    repo = TradeRepo()
    today = date.today().strftime("%Y-%m-%d")
    repo.record_daily_equity(today, 4500.0, 500.0)
    dd = repo.get_max_drawdown()
    assert dd >= 0, f"invalid drawdown: {dd}"

# ── C3: tracker 累积收益 ──
def t_c3():
    from quant.benchmark.tracker import get_tracking_summary
    r = get_tracking_summary()
    assert "available" in r, f"unexpected keys: {list(r.keys())}"

# ── H5: Kelly ──
def t_h5():
    from quant.optimizer.kelly import compute_kelly_fractions, compute_lot_allocation
    # 验证函数存在且可调用 (完整参数需 _require_cfg, 此处仅冒烟)
    assert callable(compute_kelly_fractions)
    assert callable(compute_lot_allocation)

# ── H7: average_cost ──
def t_h7():
    from quant.data.trade_repo import TradeRepo
    repo = TradeRepo()
    cost = repo.get_average_cost("quant", "600519")
    assert isinstance(cost, float)

# ── M3: open_position_cost ──
def t_m3():
    from quant.data.trade_repo import TradeRepo
    repo = TradeRepo()
    cost = repo.get_open_position_cost("quant")
    assert isinstance(cost, float)

# ── Imports + config ──
def t_imports():
    from quant.monitor.alerts import check_alerts
    from quant.monitor.report import generate_report
    from quant.risk.constraints import apply_all_filters
    from quant.config.loader import load
    load()
    from quant.config.constants import _require_cfg
    assert _require_cfg("data.batch_size") > 0

print("test-v190 smoke test\n" + "="*50)
check("C2  Ledoit-Wolf shrinkage", t_c2)
check("H6  Iterative weight clip", t_h6)
check("H3  Daily equity table",   t_h3)
check("C3  Benchmark tracker",    t_c3)
check("H5  Kelly allocation",     t_h5)
check("H7  Average cost",         t_h7)
check("M3  Open position cost",   t_m3)
check("Imports + config",         t_imports)

print("="*50)
if failed == 0:
    print("ALL 8/8 PASSED ✅")
else:
    print(f"{failed} FAILED ❌")
sys.exit(failed)
