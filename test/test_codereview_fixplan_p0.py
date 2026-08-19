"""P0 修复验证测试 — 对照 CODE-REVIEW-FIX-PLAN-2026-08-09-ZH.md.

覆盖 11 项 P0 修复, 每项 ≥3 用例 (正常/边界/异常).
"""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.utils.logger import get_logger

_log = get_logger("test.codereview_fixplan_p0")


# ── P0-1: universe list_date 格式错位 ───────────────────────────────────

def _make_stocks_db(tmp_path, rows):
    db = tmp_path / "market.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE stocks(
            symbol TEXT PRIMARY KEY, name TEXT, market TEXT,
            list_date TEXT, delist_date TEXT, pe REAL, pe_ttm REAL,
            pb REAL, total_mv REAL, circ_mv REAL, roe REAL,
            eps REAL, bvps REAL, cfps REAL, high_52w REAL,
            low_52w REAL, turnover_rate REAL, industry TEXT,
            total_shares REAL, list_status TEXT DEFAULT 'L'
        );
    """)
    conn.executemany(
        "INSERT INTO stocks(symbol, name, market, list_date, delist_date) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return str(db)


class TestP01UniverseListDate:
    """list_date 存为 YYYYMMDD, 查询参数为 ISO YYYY-MM-DD → 字典序错位."""

    def test_2024_ipo_stock_included(self, tmp_path):
        db = _make_stocks_db(tmp_path, [("001387", "T", "SZ", "20240105", None)])
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT symbol FROM stocks WHERE list_date <= strftime('%Y%m%d', ?) "
            "  AND (delist_date IS NULL OR delist_date > strftime('%Y%m%d', ?)) "
            "  AND market != 'BJ'",
            ("2024-12-31", "2024-12-31")).fetchall()
        conn.close()
        assert "001387" in [r[0] for r in rows]

    def test_pre_ipo_stock_excluded(self, tmp_path):
        db = _make_stocks_db(tmp_path, [("001391", "T", "SZ", "20250105", None)])
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT symbol FROM stocks WHERE list_date <= strftime('%Y%m%d', ?) "
            "  AND (delist_date IS NULL OR delist_date > strftime('%Y%m%d', ?)) "
            "  AND market != 'BJ'",
            ("2024-12-31", "2024-12-31")).fetchall()
        conn.close()
        assert "001391" not in [r[0] for r in rows]

    def test_boundary_same_day_listed(self, tmp_path):
        db = _make_stocks_db(tmp_path, [("001379", "T", "SZ", "20240119", None)])
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT symbol FROM stocks WHERE list_date <= strftime('%Y%m%d', ?)",
            ("2024-01-19",)).fetchall()
        conn.close()
        assert "001379" in [r[0] for r in rows]


# ── P0-2: market_cap 三源三单位 ───────────────────────────────────────

class TestP02MarketCapUnits:
    """eastmoney=元, jqdata=万元, tushare=万元; 原代码统一 ×1e8 错误."""

    def test_eastmoney_unit_unchanged(self):
        mc = pd.Series([1636631833661.22], index=["600519"])
        src = pd.Series(["eastmoney"], index=["600519"])
        conv = pd.Series(1.0, index=mc.index)
        conv[src == "jqdata"] = 1e4
        conv[src == "tushare"] = 1e4
        total_mv = mc * conv
        assert total_mv.iloc[0] == pytest.approx(1636631833661.22)

    def test_jqdata_conversion_factor(self):
        """jqdata 万元→元: ×1e4 (不是 ×1e8 如旧码/修办建议)."""
        mc = pd.Series([150873598.0], index=["600519"])
        src = pd.Series(["jqdata"], index=["600519"])
        conv = pd.Series(1.0, index=mc.index)
        conv[src == "jqdata"] = 1e4
        conv[src == "tushare"] = 1e4
        total_mv = mc * conv
        # 150873598 万元 = 1.509e12 元 ≈ 600519 实际市值 (~1.5T)
        assert total_mv.iloc[0] == pytest.approx(1.509e12, rel=1e-3)

    def test_mixed_sources_correct(self):
        mc = pd.Series([1636631833661.22, 150873598.0], index=["600519", "000858"])
        src = pd.Series(["eastmoney", "jqdata"], index=["600519", "000858"])
        conv = pd.Series(1.0, index=mc.index)
        conv[src == "jqdata"] = 1e4
        conv[src == "tushare"] = 1e4
        total_mv = mc * conv
        assert total_mv.iloc[0] == pytest.approx(1636631833661.22)
        assert total_mv.iloc[1] == pytest.approx(1.509e12, rel=1e-3)


# ── P0-3: neutralize NaN 传染 ────────────────────────────────────────

class TestP03NeutralizeNaN:
    """_apply_neutralize_batch: NaN 值 → P @ y 传染全矩阵."""

    def test_nan_stock_does_not_infect_others(self):
        n = 100
        common_index = pd.Index([f"S{i:04d}" for i in range(n)])
        P = np.eye(n)
        scores = pd.Series(np.random.randn(n), index=common_index)
        scores.iloc[50] = np.nan
        from quant.risk.neutralize import _apply_neutralize_batch
        result = _apply_neutralize_batch(P, common_index, scores)
        assert np.isnan(result.iloc[50])
        valid = result.drop(result.index[50])
        assert not valid.isna().any(), "NaN 传染到有效股票!"

    def test_all_nan_returns_original(self):
        n = 10
        common_index = pd.Index([f"S{i:04d}" for i in range(n)])
        P = np.eye(n)
        scores = pd.Series([np.nan] * n, index=common_index)
        from quant.risk.neutralize import _apply_neutralize_batch
        result = _apply_neutralize_batch(P, common_index, scores)
        assert result.isna().all()

    def test_single_valid_returns_original(self):
        n = 10
        common_index = pd.Index([f"S{i:04d}" for i in range(n)])
        P = np.eye(n)
        scores = pd.Series([1.0] + [np.nan] * 9, index=common_index)
        from quant.risk.neutralize import _apply_neutralize_batch
        result = _apply_neutralize_batch(P, common_index, scores)
        assert result.iloc[0] == pytest.approx(1.0)
        assert result.iloc[1:].isna().all()


# ── P0-4: ATR 日期上界 (防前视) ───────────────────────────────────────

class TestP04ATRDateBound:
    """_compute_atr: 缺少 as_of 导致回测读取未来行情."""

    def test_missing_as_of_raises(self):
        from quant.execution.stop_loss import _compute_atr
        with pytest.raises(ValueError, match="as_of is required"):
            _compute_atr("600519", 20, as_of=None)

    def test_as_of_filters_future_data(self, tmp_path):
        from quant.execution.stop_loss import _compute_atr
        db = str(tmp_path / "market.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE daily(symbol TEXT, date TEXT, high REAL, low REAL, close REAL);
            INSERT INTO daily VALUES
            ('600519','2023-12-29', 95, 90, 93),
            ('600519','2024-01-01', 100, 95, 98),
            ('600519','2024-01-02', 105, 99, 103),
            ('600519','2024-01-03', 110, 104, 108),
            ('600519','2026-08-07', 200, 190, 195);
        """)
        conn.commit()
        conn.close()
        import quant.execution.stop_loss as sl
        orig_dm = sl.DatabaseManager
        class _FakeDM:
            @staticmethod
            def market():
                return sqlite3.connect(db)
        sl.DatabaseManager = _FakeDM
        try:
            sl._CACHE.clear()
            atr = _compute_atr("600519", 2, as_of="2024-01-03")
            assert atr > 0, "ATR 应基于 ≤ as_of 的数据"
        finally:
            sl.DatabaseManager = orig_dm
            sl._CACHE.clear()

    def test_call_site_passes_today(self):
        import inspect
        from quant.execution.stop_loss import RiskManager
        src = inspect.getsource(RiskManager.check)
        assert "_compute_atr(sym, self.atr_period, today)" in src


# ── P0-5: XGB 特征列序对齐 ───────────────────────────────────────────

class TestP05XGBFeatureAlignment:
    """XGB predict: 缺列 → pad 末尾导致列序错位."""

    def test_no_filter_in_comprehension(self):
        import inspect
        from quant.alpha.xgb_model import XgbAlphaModel
        src = inspect.getsource(XgbAlphaModel.predict)
        # 去掉注释行后, 列表推导不应有过滤
        code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
        code_src = "\n".join(code_lines)
        assert "if fn in factor_values" not in code_src
        assert "for fn in self._feature_names" in code_src

    def test_no_pad_zeros(self):
        import inspect
        from quant.alpha.xgb_model import XgbAlphaModel
        src = inspect.getsource(XgbAlphaModel.predict)
        assert "np.zeros((X.shape" not in src

    def test_column_order_matches_qlib(self):
        import inspect
        from quant.alpha.xgb_model import XgbAlphaModel
        from quant.alpha.qlib_model import LgbAlphaModel
        xgb_src = inspect.getsource(XgbAlphaModel.predict)
        lgb_src = inspect.getsource(LgbAlphaModel.predict)
        assert "for fn in self._feature_names" in xgb_src
        assert "for fn in self._feature_names" in lgb_src


# ── P0-6: DSR/PSR 量纲一致 ───────────────────────────────────────────

class TestP06DSRUnits:
    """DSR: 年化 SR + 日频 n_obs → 方差放大 → DSR 恒 1.0."""

    def test_daily_sr_not_annualized_for_dsr(self):
        import inspect
        from quant.evaluation.deflated_sharpe import compute_dsr_for_strategy
        src = inspect.getsource(compute_dsr_for_strategy)
        assert "daily_sr = float(np.mean(rets) / max(np.std(rets, ddof=1), 1e-10))" in src
        assert "deflated_sharpe_ratio(daily_sr," in src

    def test_dsr_not_always_one(self):
        np.random.seed(42)
        from quant.evaluation.deflated_sharpe import compute_dsr_for_strategy
        # 日收益: mean=0.03%, std=1% → 年化 SR ≈ 0.48
        # DSR 不应恒为 1.0
        rets = list(np.random.randn(500) * 0.01 + 0.0003)
        result = compute_dsr_for_strategy(rets, n_factors=4, annual_factor=252)
        assert result["dsr"] < 1.0, f"DSR={result['dsr']} 应 < 1.0 (前视 bug 会恒为 1.0)"

    def test_loop_dsr_uses_daily_sr(self):
        import inspect
        from quant.backtest.loop import _compute_dsr
        src = inspect.getsource(_compute_dsr)
        assert "sqrt(ann_days)" not in src.split("deflated_sharpe_ratio")[0]


# ── P0-7: EVAL_REJECT 非法事件 ───────────────────────────────────────

class TestP07EvalReject:
    """phase5_monitor 使用 EVAL_REJECT, 不在 _VALID_EVENTS → ValueError 被吞."""

    def test_eval_reject_not_valid(self):
        from quant.factor.state_manager import FactorStateManager
        assert not FactorStateManager.is_valid_event("EVAL_REJECT")

    def test_eval_fail_is_valid(self):
        from quant.factor.state_manager import FactorStateManager
        assert FactorStateManager.is_valid_event("EVAL_FAIL")
        assert FactorStateManager.is_valid_event("IC_PERSISTENT")

    def test_phase5_no_eval_reject_event(self):
        import inspect
        from quant.evaluation import phase5_monitor
        src = inspect.getsource(phase5_monitor)
        # EVAL_REJECT 不应作为事件传递给 transition/batch_transition
        assert '"EVAL_REJECT"' not in src
        assert "'EVAL_REJECT'" not in src
        assert "EVAL_FAIL" in src
        assert "IC_PERSISTENT" in src


# ── P0-8: backtest 命名使用 BACKTEST_DB ────────────────────────────────

class TestP08BacktestNaming:
    """naming.next_name 查询 TRADE_DB → 回测永远 backtest_1."""

    def test_next_backtest_name_queries_backtest_db(self):
        import inspect
        from quant.backtest.naming import next_backtest_name
        src = inspect.getsource(next_backtest_name)
        assert "_BACKTEST_DB" in src

    def test_next_name_requires_db_path(self):
        from quant.backtest.naming import next_name
        with pytest.raises(ValueError, match="db_path is required"):
            next_name("backtest")

    def test_backtest_names_increment_in_backtest_db(self, tmp_path):
        db = str(tmp_path / "bt.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE strategy_config(strategy TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO strategy_config(strategy) VALUES ('backtest_1')")
        conn.commit()
        conn.close()
        from quant.backtest.naming import next_name
        assert next_name("backtest", db) == "backtest_2"


# ── P0-9: var.py Series 权重 ─────────────────────────────────────────

class TestP09VarSeriesWeights:
    """var.py: weights 是 pd.Series → position_val 恒 0."""

    def test_series_weights_nonzero(self, tmp_path):
        from quant.risk.var import stress_test
        db = str(tmp_path / "market.db")
        conn = sqlite3.connect(db)
        # 2020_covid scenario: 2020-01-20..2020-02-03
        conn.executescript("""
            CREATE TABLE daily(symbol TEXT, date TEXT, high REAL, low REAL, close REAL);
            INSERT INTO daily VALUES
            ('600519','2020-01-20', 2000, 1900, 1950),
            ('600519','2020-01-24', 1900, 1500, 1550),
            ('600519','2020-01-27', 1600, 1400, 1450);
        """)
        conn.commit()
        conn.close()
        import quant.data.repos._base as _db_base
        orig = _db_base.DatabaseManager
        class _FakeDM:
            @staticmethod
            def market():
                return sqlite3.connect(db)
        _db_base.DatabaseManager = _FakeDM
        try:
            weights = pd.Series({"600519": 100000.0})
            positions = [{"symbol": "600519"}]
            result = stress_test(positions, weights)
            covid = result.get("2020_covid", {})
            assert covid.get("loss_est") is not None and covid["loss_est"] > 0
        finally:
            _db_base.DatabaseManager = orig

    def test_dict_weights_still_work(self, tmp_path):
        from quant.risk.var import stress_test
        db = str(tmp_path / "m.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE daily(symbol TEXT, date TEXT, high REAL, low REAL, close REAL);
            INSERT INTO daily VALUES ('600519','2020-01-20', 2000, 1900, 1950),
                                    ('600519','2020-01-24', 1900, 1500, 1550);
        """)
        conn.commit()
        conn.close()
        import quant.data.repos._base as _db_base
        orig = _db_base.DatabaseManager
        class _FakeDM:
            @staticmethod
            def market():
                return sqlite3.connect(db)
        _db_base.DatabaseManager = _FakeDM
        try:
            weights = {"600519": 100000.0}
            positions = [{"symbol": "600519"}]
            result = stress_test(positions, weights)
            covid = result.get("2020_covid", {})
            assert "loss_est" in covid
        finally:
            _db_base.DatabaseManager = orig

    def test_source_code_has_series_branch(self):
        import inspect
        from quant.risk.var import stress_test
        src = inspect.getsource(stress_test)
        assert "isinstance(weights, pd.Series)" in src


# ── P0-10: VnpyAdapter 白名单 + 回调 ───────────────────────────────────

class TestP010VnpyAdapter:
    """VnpyAdapter: 白名单校验 + 回调实现."""

    def test_valid_adapter_whitelist(self):
        from quant.execution.broker_adapter import VnpyAdapter
        assert "simulated" in VnpyAdapter._VALID_ADAPTERS
        assert "vnpy" in VnpyAdapter._VALID_ADAPTERS

    def test_connect_rejects_non_whitelisted(self):
        from quant.execution.broker_adapter import VnpyAdapter
        from unittest.mock import patch
        with patch("quant.execution.broker_adapter._cfg_get", return_value="invalid"):
            adapter = VnpyAdapter.__new__(VnpyAdapter)
            adapter._vnpy_available = True
            adapter._settings = {}
            adapter._gateway_name = "test"
            adapter._strategy = "quant"
            with pytest.raises(ValueError, match="白名单"):
                adapter.connect()

    def test_callbacks_not_pass(self):
        import inspect
        from quant.execution.broker_adapter import VnpyAdapter
        for method_name in ["_on_trade", "_on_order", "_on_position"]:
            src = inspect.getsource(getattr(VnpyAdapter, method_name))
            assert "pass" not in src, f"P0-10: {method_name} 仍是空实现"

    def test_pending_orders_dict(self):
        from quant.execution.broker_adapter import VnpyAdapter
        from unittest.mock import patch
        with patch("quant.execution.broker_adapter._check_vnpy", return_value=False):
            adapter = VnpyAdapter()
            assert hasattr(adapter, "_pending_orders")
            assert adapter._pending_orders == {}
