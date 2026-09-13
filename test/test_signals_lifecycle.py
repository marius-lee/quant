"""v625 regression: signals._run() must finish task_runs on BOTH success and crash.

Templates: TDD (3) + defensive-programming (2) + observability (8).
Mirrors test/test_scheduler_status_c10.py: monkeypatch source-module bindings and
use a _FakeMetrics; function-level imports inside signals._run() are patched at
their SOURCE modules.

Validates Bug B1 fix (V586 try/except/finally in signals._run) — the direct root
cause of the 2026-09-08 signals task stuck 'running' forever.
"""
import pytest

import quant.pipeline as pipeline_mod
import quant.factor.store as store_mod
import quant.backtest.context as ctx_mod
import quant.execution.stop_loss as stop_mod
import quant.scheduler.signals as signals_mod


class _FakeMetrics:
    def __init__(self):
        self.inc_calls = []

    def inc(self, key, n=1):
        self.inc_calls.append(key)


class _FakeFactorStore:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


class _FakeContext:
    def __init__(self, *a, **k):
        pass


class _FakeRiskManager:
    def __init__(self, *a, **k):
        pass

    def get_cooloff_symbols(self, today):
        return []


def _ok_generate_signals(date_str, skip_pull=False, factor_store=None,
                         exclude_symbols=None, ctx=None):
    return {"target_positions": [
        {"symbol": "000001"}, {"symbol": "000002"}, {"symbol": "000003"},
    ]}


def _patch_modules(monkeypatch):
    """Patch source-module bindings used by signals._run's function-level imports."""
    monkeypatch.setattr(pipeline_mod, "generate_signals", _ok_generate_signals)
    monkeypatch.setattr(store_mod, "FactorStore", _FakeFactorStore)
    monkeypatch.setattr(ctx_mod, "ExecutionContext", _FakeContext)
    monkeypatch.setattr(stop_mod, "RiskManager", _FakeRiskManager)


def _patch_tasklog(monkeypatch, calls, start_rid=1):
    """Patch signals._run's module-level _tk_start/_tk_finish bindings (c10 pattern)."""
    fm = _FakeMetrics()
    monkeypatch.setattr(signals_mod, "_m", fm)

    def _start(*a, **k):
        calls["start"] = a
        return start_rid

    def _finish(task, date, status, error=None, summary=None):
        calls["finish"] = (task, date, status, error, summary)

    monkeypatch.setattr(signals_mod, "_tk_start", _start)
    monkeypatch.setattr(signals_mod, "_tk_finish", _finish)
    return fm


def test_signals_success_finishes_ok(monkeypatch):
    """Happy path: crash-free run -> task_runs 'ok' + ok metric + return dict."""
    calls = {}
    fm = _patch_tasklog(monkeypatch, calls, start_rid=1)
    _patch_modules(monkeypatch)

    result = signals_mod._run("2026-08-20")

    assert calls["start"], "signals._run should call _tk_start (legacy + Dagster)"
    assert calls["finish"][0] == "signals"
    assert calls["finish"][1] == "2026-08-20"
    assert calls["finish"][2] == "ok"          # <-- the lifecycle guarantee
    assert calls["finish"][4]["targets"] == 3  # summary.targets
    assert "scheduler.signals.ok" in fm.inc_calls
    assert result["targets"] == 3


def test_signals_crash_finishes_failed(monkeypatch):
    """Crash path: exception must NOT leave task_runs 'running' -> must be 'failed'."""
    calls = {}
    fm = _patch_tasklog(monkeypatch, calls, start_rid=1)
    _patch_modules(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("generate_signals exploded")
    monkeypatch.setattr(pipeline_mod, "generate_signals", _boom)

    with pytest.raises(RuntimeError, match="generate_signals exploded"):
        signals_mod._run("2026-08-20")

    # The bug: without try/finally this finish() never ran -> row stuck 'running'.
    assert calls["finish"][2] == "failed"
    assert calls["finish"][3] == "generate_signals exploded"
    assert "scheduler.signals.failed" in fm.inc_calls


def test_signals_dedup_skip_no_finish(monkeypatch):
    """dedup (rid=None) -> short-circuit without touching task_runs."""
    calls = {}
    fm = _patch_tasklog(monkeypatch, calls, start_rid=None)
    _patch_modules(monkeypatch)

    result = signals_mod._run("2026-08-20")

    assert result == {"targets": 0, "elapsed": 0.0}
    assert "finish" not in calls          # no _tk_finish on a dedup skip
    assert not fm.inc_calls                # no metric side-effects
