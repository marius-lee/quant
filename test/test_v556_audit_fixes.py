"""v556 全量自查回归修复测试 (2026-08-19).

覆盖: D1 回测 metrics n_trials 传参 (loop.py NameError),
E1 stop_loss 同日卖出后买回判定, E5 HRP n≤2 零方差,
F5 task_log.finish 覆盖 'lunch' stage 行, E2 iterative_clip 稀疏告警不回归."""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd
import pytest


def _trade_db(tmp_path, monkeypatch):
    """隔离 trades.db: monkeypatch trade_repo 模块的 TRADE_DB 常量."""
    import os
    import quant.data.repos.trade_repo as tr_mod
    db = os.path.join(str(tmp_path), "trades_v556.db")
    monkeypatch.setattr(tr_mod, "TRADE_DB", db)
    return db


class TestBacktestMetricsNtrials:
    """D1: _compute_backtest_metrics 模块级函数不得引用 run_backtest 局部变量."""

    def test_metrics_with_n_trials(self):
        from quant.backtest.loop import _compute_backtest_metrics
        curve = []
        eq = 10000.0
        for i in range(60):
            eq *= 1 + (0.001 * ((i % 3) - 1))
            curve.append({"date": f"2024-01-{i + 1:02d}", "equity": eq})
        m = _compute_backtest_metrics(curve, None, n_trials=104)
        assert m["sharpe"] != 0
        assert m["final_equity"] > 0
        assert "dsr" in m


class TestStopLossSameDayRebuy:
    """E1: 同日先卖后买 (TP2 清仓→当日买回) 必须识别为新仓 (v554 回归)."""

    def test_same_day_rebuy_detected(self, tmp_path, monkeypatch):
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        repo = TradeRepo()
        # 同日: 先卖后买 — created_at 写入先后 = 业务先后
        repo.record_trade("quant", "2026-08-19", "600000", "sell", 12.0, 100, conn=None)
        repo.record_trade("quant", "2026-08-19", "600000", "buy", 11.0, 100, conn=None)
        from quant.execution.stop_loss import RiskManager
        rm = RiskManager(strategy="quant")
        # 修复前: buy_time[:10] == last_sell[:10] 恒 False → 旧仓 meta 污染新仓
        assert rm._is_recently_rebought("600000", "2026-08-19") is True

    def test_same_day_buy_then_sell_not_rebought(self, tmp_path, monkeypatch):
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        repo = TradeRepo()
        # 同日先买后卖 → 无"卖出后的买回", 不应误判 (created_at 先买后卖)
        repo.record_trade("quant", "2026-08-19", "600000", "buy", 11.0, 100, conn=None)
        repo.record_trade("quant", "2026-08-19", "600000", "sell", 12.0, 100, conn=None)
        from quant.execution.stop_loss import RiskManager
        rm = RiskManager(strategy="quant")
        assert rm._is_recently_rebought("600000", "2026-08-19") is False

    def test_cross_day_rebuy_still_detected(self, tmp_path, monkeypatch):
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        repo = TradeRepo()
        repo.record_trade("quant", "2026-08-18", "600000", "sell", 12.0, 100, conn=None)
        repo.record_trade("quant", "2026-08-19", "600000", "buy", 11.0, 100, conn=None)
        from quant.execution.stop_loss import RiskManager
        rm = RiskManager(strategy="quant")
        assert rm._is_recently_rebought("600000", "2026-08-19") is True


class TestHrpN2ZeroVariance:
    """E5: n≤2 早退不得绕过零方差压 0."""

    def test_n2_zero_variance_deprioritized(self):
        from quant.optimizer.hrp import hrp_weights
        cov = np.array([[0.0004, 0.0],
                        [0.0, 0.0]])
        w = hrp_weights(cov)
        assert w.shape == (2,)
        assert w[1] == pytest.approx(0.0, abs=1e-12)
        assert w.sum() == pytest.approx(1.0)

    def test_n2_normal_equal_weight(self):
        from quant.optimizer.hrp import hrp_weights
        cov = np.array([[0.0004, 0.0],
                        [0.0, 0.0009]])
        w = hrp_weights(cov)
        assert w.sum() == pytest.approx(1.0)
        assert w[0] > 0 and w[1] > 0


class TestTaskLogLunchFinish:
    """F5: finish 必须能覆盖 monitor 'lunch' stage 行 (午休崩溃 failed 落库)."""

    @pytest.fixture
    def _tmp_conn(self, tmp_path, monkeypatch):
        import sqlite3
        import quant.scheduler.task_log as tl
        db = str(tmp_path / "task_runs.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS task_runs ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT, date TEXT, "
                     "started_at TEXT, finished_at TEXT, status TEXT, error TEXT, "
                     "summary TEXT, pid INTEGER)")
        conn.commit()
        monkeypatch.setattr(tl, "_conn", lambda: sqlite3.connect(db))
        return db

    def test_finish_overrides_lunch(self, _tmp_conn):
        import quant.scheduler.task_log as tl
        rid = tl.start("monitor", "2026-08-19", grace_seconds=21600)
        assert rid is not None
        c = tl._conn()
        c.execute("UPDATE task_runs SET status='lunch' WHERE id=?", (rid,))
        c.commit()
        c.close()
        tl.finish("monitor", "2026-08-19", "failed",
                  error="monitor daemon crashed during lunch break")
        c = tl._conn()
        row = c.execute("SELECT status FROM task_runs WHERE id=?",
                        (rid,)).fetchone()
        c.close()
        assert row[0] == "failed"

    def test_finish_ok_overrides_lunch(self, _tmp_conn):
        import quant.scheduler.task_log as tl
        rid = tl.start("monitor", "2026-08-19", grace_seconds=21600)
        c = tl._conn()
        c.execute("UPDATE task_runs SET status='lunch' WHERE id=?", (rid,))
        c.commit()
        c.close()
        tl.finish("monitor", "2026-08-19", "ok")
        c = tl._conn()
        row = c.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()
        c.close()
        assert row[0] == "ok"


class TestIterativeClipSparse:
    """E2: 稀疏正权 (free 集全 0) 不得 NaN/死循环, 返回 Σ<1 且告警 (不静默)."""

    def test_sparse_all_at_limit_no_crash(self):
        from quant.optimizer.portfolio import _iterative_clip
        # 30 只, 12 只已到限 → n×max_single=1.5 ≥1 可行约束, 稀疏停滞场景
        w = np.zeros(30)
        w[:12] = 0.05
        out = _iterative_clip(w, max_single=0.05)
        assert np.all(np.isfinite(out))
        assert out.sum() <= 1.0 + 1e-9
        assert np.all(out <= 0.05 + 1e-9)
        # 正权股已到限 → Σ = 0.60 保留现金 (稀疏 MV 合法状态, 显式告警)
        assert out.sum() == pytest.approx(0.6)
