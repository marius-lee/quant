# v538 tests — 回测默认区间接入 config (default_start/default_end):
# 原 loop.py full 模式 start=None → end-12mo, 与 config 语义脱节
# (default_start=2020-01-01 物化同源起点从未被消费).
import re

import yaml


def _load_loop_src() -> str:
    return open("quant/backtest/loop.py", encoding="utf-8").read()


def _load_backtest_cfg() -> dict:
    with open("quant/config/config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["backtest"]


# ── 1. 源码接线断言 ──
def test_full_mode_defaults_from_config():
    src = _load_loop_src()
    assert "backtest.default_start" in src
    assert "backtest.default_end" in src
    # 旧逻辑 (end-12mo) 不得残留
    assert "DateOffset(months=12)" not in src.replace("max_days", "")


def test_smoke_mode_unaffected():
    src = _load_loop_src()
    # smoke 分支仍可用 datetime.now (其语义就是"最近 1 个月")
    assert "months=1" in src


# ── 2. 配置一致性: 回测起点与物化起点同源 (v473 约定, 勿改) ──
def test_backtest_start_aligns_factor_cache_start():
    cfg = _load_backtest_cfg()
    assert cfg["default_start"] == "2020-01-01"
    assert cfg["default_start"] == cfg["factor_cache_start"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg["default_end"])


# ── 3. 默认区间解析逻辑 (不真跑回测, 只验解析函数) ──
def test_default_range_uses_config_values(monkeypatch):
    from quant.backtest import loop

    seen = {}

    def fake_require_cfg(key, **kw):
        seen[key] = True
        return "2020-01-01" if key == "backtest.default_start" else "2026-06-30"

    import quant.config.constants as constants
    monkeypatch.setattr(constants, "_require_cfg", fake_require_cfg)
    monkeypatch.setattr(loop, "_require_cfg", fake_require_cfg)

    # 直接走 full 分支的默认解析 (绕过真回测: 替换重依赖为假类)
    calls = {}

    class FakeEngine:
        def __init__(self, *a, **k):
            raise RuntimeError("not executed")

    # loop.py 在函数体内 import 各重依赖, 须 patch 源模块属性
    import quant.data.store as ds
    import quant.execution.engine as ee
    import quant.execution.cost as cm
    import quant.factor.store as fs
    monkeypatch.setattr(ds, "DataStore", FakeEngine)
    monkeypatch.setattr(fs, "FactorStore", FakeEngine)
    monkeypatch.setattr(ee, "ExecutionEngine", FakeEngine)
    monkeypatch.setattr(cm, "CostModel", FakeEngine)

    # 拦截真正执行回测前的解析: 解析发生在 engine 构造前, 用异常中止后断言 seen
    try:
        loop.run_backtest(mode="full", strategy="v538_probe")
    except RuntimeError:
        pass  # FakeEngine 被构造 = 解析已走完, 断言 seen
    assert seen.get("backtest.default_start") is True
    assert seen.get("backtest.default_end") is True
