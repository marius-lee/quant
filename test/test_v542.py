# v542 tests — 恒空结果因子物化排除:
# fund_change (245 天 blocked, 2020-12-31 起) / financial_anomaly (184 天,
# 2023-03-31 起) 依赖数据特定日期区间不可用, force 全量重算独立复现空结果
# (2026-08-19 物化 500 只完整复刻实证) → 排除物化池避免每轮重算浪费 + blocked 刷屏.
# 排除仅作用于物化池 (materialize_full.sh + 晚间链 factor_cache),
# registry 状态不变 (evaluating 仍可单独评估); 数据补齐后从 config
# factor.materialize_exclude 移除即自动恢复.
from quant.config.constants import _require_cfg
from quant.factor.compute import get_factor_names


# ── 1. config 排除列表存在且非空 ──
def test_config_exclude_list():
    excl = _require_cfg("factor.materialize_exclude")
    assert isinstance(excl, list) and excl
    assert "fund_change" in excl and "financial_anomaly" in excl


# ── 2. get_factor_names exclude 生效 (backtesting 池) ──
def test_exclude_applied_to_backtesting_pool():
    excl = _require_cfg("factor.materialize_exclude")
    all_b = get_factor_names(status_filter="backtesting")
    filtered = get_factor_names(status_filter="backtesting", exclude=excl)
    assert all(x in all_b for x in excl)          # 池内确实存在
    assert all(x not in filtered for x in excl)   # 排除后消失
    assert len(filtered) == len(all_b) - len(excl)


# ── 3. exclude 默认 None 不改变现有行为 (调用方兼容) ──
def test_exclude_default_noop():
    assert get_factor_names(status_filter="backtesting") \
        == get_factor_names(status_filter="backtesting", exclude=None)


# ── 4. using 池也排除 (晚间链并集口径) ──
def test_exclude_applied_to_using_pool():
    excl = _require_cfg("factor.materialize_exclude")
    using = get_factor_names(status_filter="using", exclude=excl)
    for f in excl:
        if f in get_factor_names(status_filter="using"):
            assert f not in using