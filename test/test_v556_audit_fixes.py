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


class TestKellyResidualCap:
    """E3: 残差回收不得突破 max_single 集中度 (Small 层 5%×capital)."""

    def test_recycle_respects_max_single(self):
        from quant.optimizer.kelly import compute_lot_allocation
        n = 5
        alpha = pd.Series([1.0] * n, index=[f"s{i}" for i in range(n)])
        prices = pd.Series([10.0] * n, index=alpha.index)
        capital = 100000.0
        lots, cash = compute_lot_allocation(alpha, prices, capital=capital,
                                            ic_map=None, lot_size=100)
        # kelly 等权: 0.2 × fraction(0.5) = 0.1 → clip(max_single=0.05) → 5 手/只
        max_mv = 0.05 * capital
        assert cash >= 0
        assert lots.sum() > 0
        for sym in lots.index:
            mv = lots[sym] * prices[sym] * 100
            # 修复前: 残差回收无守卫, 每只补到 20 手 (20000 = 20% > 5000)
            assert mv <= max_mv + 1e-6, \
                f"{sym} 市值 {mv} > max_single 上限 {max_mv}"


class TestSubprocessTimeoutGuard:
    """F1: _wait_done 超时看护 — 挂死子进程不得永久冻结 orchestrator 主循环."""

    def _spec(self, timeout_s):
        from datetime import time
        from quant.scheduler.manifest import TaskSpec
        return TaskSpec(name="evening_chain", label="x", schedule="19:00",
                        window=(time(19, 0), time(23, 59)),
                        timeout_s=timeout_s, mode="subprocess")

    def test_wait_done_timeout_terminates(self, monkeypatch):
        import quant.scheduler.runners as runners
        from quant.scheduler.runners import SubprocessRunner
        runner = SubprocessRunner("2026-08-19")

        class FakeProc:
            def poll(self):
                return None  # 永不退出

        runner._proc = FakeProc()
        cleaned = []
        runner.cleanup = lambda: cleaned.append(1)
        monkeypatch.setattr(runners, "POLL", 0.05)
        runner._wait_done(self._spec(timeout_s=1))
        assert cleaned == [1], "超时后必须 terminate"
        assert runner._last_rc == 1
        assert runner._proc is None

    def test_wait_done_success_returns(self, monkeypatch):
        import quant.scheduler.runners as runners
        from quant.scheduler.runners import SubprocessRunner
        runner = SubprocessRunner("2026-08-19")
        runner._proc = None
        monkeypatch.setattr(runners, "POLL", 0.05)
        runner._wait_done(self._spec(timeout_s=1))
        assert runner._last_rc is None

    def test_wait_done_no_timeout_spec_no_deadline(self, monkeypatch):
        import quant.scheduler.runners as runners
        from quant.scheduler.runners import SubprocessRunner
        runner = SubprocessRunner("2026-08-19")
        runner._proc = None
        monkeypatch.setattr(runners, "POLL", 0.05)
        # timeout_s=None → deadline 无效, 只轮询一次即返回
        runner._wait_done(self._spec(timeout_s=None))
        assert runner._last_rc is None


class TestSubprocessNoInternalRespawn:
    """F2: 移除内部 respawn — 重试预算归 orchestrator 单一真相源."""

    def _spec(self):
        from datetime import time
        from quant.scheduler.manifest import TaskSpec
        return TaskSpec(name="evening_chain", label="x", schedule="19:00",
                        window=(time(19, 0), time(23, 59)), mode="subprocess")

    def test_failed_subprocess_not_respawned(self, monkeypatch):
        import quant.scheduler.runners as runners
        from quant.scheduler.runners import SubprocessRunner
        runner = SubprocessRunner("2026-08-19")

        class FakeProc:
            def poll(self):
                return 1  # 失败退出

        runner._proc = FakeProc()
        spawns = []
        runner._run_subprocess = lambda s: spawns.append(1)
        monkeypatch.setattr(runners, "_cleanup_evening_children", lambda today: None)
        runner._wait_subprocess(self._spec())
        assert spawns == [], "失败后不得在内部再次 respawn (v532 双预算问题)"
        assert runner._last_rc == 1
        assert runner._proc is None


class TestStatsCacheEvalStart:
    """D3: eval_start 注入取更早起点 (min) — 12 个月训练窗口真正生效."""

    def test_eval_start_takes_earlier_window(self, monkeypatch):
        import quant.data.store as ds_mod
        calls = []

        class FakeRows:
            def __init__(self, rows):
                self._r = rows

            def fetchall(self):
                return self._r

        class FakeConn:
            def execute(self, sql, params=()):
                calls.append((sql, params))
                return FakeRows([])

            def close(self):
                pass

        class FakeStore:
            def _connect(self):
                return FakeConn()

            def close(self):
                pass

        monkeypatch.setattr(ds_mod, "DataStore", FakeStore)
        from quant.factor.stats_cache import compute_factor_stats
        compute_factor_stats(symbols=["600000"], factor_names=["amount_avg"],
                             lookback=120, eval_start="2025-01-01",
                             eval_end="2026-06-30")
        q = [p for s, p in calls if "SELECT DISTINCT date" in s]
        assert q, "DISTINCT date query not executed"
        # end-180d = 2026-01-01; min(end-180d, eval_start) = 2025-01-01
        # 修复前 max() → 2026-01-01 (窗口仍 ~126 天, v554 声称修复未生效)
        assert q[0][0] == "2025-01-01"
