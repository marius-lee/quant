"""test-v302: 晚间依赖链 — daily_data → factor_cache → attribution.

覆盖: 顺序执行 / 失败中断 / 已 ok 跳过 / 重复触发去重。
"""
import logging
import pytest

from quant.scheduler import evening


@pytest.fixture
def chain(monkeypatch):
    """替换 task_log 与阶段加载, 捕获调用序列."""
    calls = []
    state = {"statuses": {}}  # {task: 最新 status}
    monkeypatch.setattr(evening, "_tk_start", lambda *a, **k: 1)
    monkeypatch.setattr(evening, "_tk_finish",
                        lambda task, date, status, error=None: calls.append(("finish", status, error)))
    monkeypatch.setattr(evening, "_tk_query",
                        lambda date: [{"task_name": k, "status": v} for k, v in state["statuses"].items()])

    def fake_loader(status_map, raise_map=None):
        def _load(module_path):
            name = module_path.rsplit(".", 1)[-1]

            class _Stage:
                @staticmethod
                def _run(*args):  # v385: factor_cache 传 (start, end), 其余传 (today,)
                    calls.append(("run", name))
                    if raise_map and name in raise_map:
                        state["statuses"][name] = "failed"
                        raise RuntimeError(f"{name} boom")
                    state["statuses"][name] = status_map.get(name, "ok")
            return _Stage
        return _load
    return calls, state, fake_loader


class TestEveningChain:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        """v378: silence evening logger (测试错误路径不打ERROR到生产日志) + mock慢速外部调用."""
        logging.getLogger("quant.scheduler.evening").setLevel(logging.CRITICAL + 1)
        # v421: ML 训练阶段 (lgb/xgb) 从链测试中剔除 — 训练重且与数据链无关
        chain_no_ml = [(n, m) for n, m in evening._CHAIN if n not in ("lgb_train", "xgb_train")]
        monkeypatch.setattr(evening, "_CHAIN", chain_no_ml)
        monkeypatch.setattr("quant.data.quality.check_daily_quality",
                           lambda date: {"date": date, "overall": "ok", "checks": {}})
        monkeypatch.setattr("quant.data.store.DataStore.sync_adj_factor",
                           lambda self, max_batches=1: {"rows": 0})

    def test_all_ok_runs_in_dependency_order(self, chain, monkeypatch):
        calls, state, fake_loader = chain
        monkeypatch.setattr(evening, "_load_stage", fake_loader({}))
        evening._run("2026-07-27")
        run_order = [c[1] for c in calls if c[0] == "run"]
        assert run_order == ["daily_data", "factor_cache", "attribution"]
        assert ("finish", "ok", None) in calls

    def test_daily_data_failure_aborts_chain(self, chain, monkeypatch):
        """daily_data 失败 → factor_cache/attribution 不启动, 链标 failed."""
        calls, state, fake_loader = chain
        monkeypatch.setattr(evening, "_load_stage", fake_loader({}, raise_map={"daily_data"}))
        evening._run("2026-07-27")
        run_order = [c[1] for c in calls if c[0] == "run"]
        assert run_order == ["daily_data"]
        finish = [c for c in calls if c[0] == "finish"][0]
        assert finish[1] == "failed"
        assert "daily_data" in finish[2]

    def test_factor_cache_failure_skips_attribution(self, chain, monkeypatch):
        """factor_cache 失败 → attribution 不启动 (G1/G4 缺缓存必崩, 不能跑)."""
        calls, state, fake_loader = chain
        monkeypatch.setattr(evening, "_load_stage", fake_loader({"factor_cache": "failed"}))
        evening._run("2026-07-27")
        run_order = [c[1] for c in calls if c[0] == "run"]
        assert run_order == ["daily_data", "factor_cache"]
        assert ("finish", "failed", "factor_cache status=failed, chain aborted (后续阶段跳过)") in calls

    def test_stage_already_ok_is_skipped(self, chain, monkeypatch):
        """人工提前跑过且 ok 的阶段不重复执行."""
        calls, state, fake_loader = chain
        state["statuses"]["daily_data"] = "ok"
        monkeypatch.setattr(evening, "_load_stage", fake_loader({}))
        evening._run("2026-07-27")
        run_order = [c[1] for c in calls if c[0] == "run"]
        assert run_order == ["factor_cache", "attribution"]

    def test_duplicate_trigger_returns_early(self, chain, monkeypatch):
        """已有 running 行 (grace 内) → 不启动任何阶段."""
        calls, state, fake_loader = chain
        monkeypatch.setattr(evening, "_tk_start", lambda *a, **k: None)
        monkeypatch.setattr(evening, "_load_stage", fake_loader({}))
        evening._run("2026-07-27")
        assert calls == []

    def test_attribution_failure_marks_chain_failed(self, chain, monkeypatch):
        """末段失败 → 链标 failed (无后续可跳过)."""
        calls, state, fake_loader = chain
        monkeypatch.setattr(evening, "_load_stage", fake_loader({}, raise_map={"attribution"}))
        evening._run("2026-07-27")
        run_order = [c[1] for c in calls if c[0] == "run"]
        assert run_order == ["daily_data", "factor_cache", "attribution"]
        finish = [c for c in calls if c[0] == "finish"][0]
        assert finish[1] == "failed"
        assert "attribution" in finish[2]


class TestChainConfig:
    def test_evening_chain_timeout_registered(self):
        """manifest.evening_chain 含超时 (僵尸检测需要, v428 替代 _TIMEOUTS)."""
        from quant.scheduler.manifest import spec
        # v430: 4h → 7.5h — 实测夜链最长 25862.7s, 原值每晚误标 aborted
        assert spec("evening_chain").timeout_s == 27000
        assert spec("evening_chain").grace_s == 27000

    def test_chain_order_is_data_then_cache_then_attribution(self):
        """依赖顺序守卫: attribution 必须在 factor_cache 之后 (防回归)."""
        names = [n for n, _ in evening._CHAIN]
        # 验证关键依赖顺序 (不要求精确列表, 因 adj_factor/lgb_train 可能增减)
        assert "daily_data" in names
        assert "factor_cache" in names
        assert "attribution" in names
        assert names.index("daily_data") < names.index("factor_cache") < names.index("attribution")
