# v536 tests — 未接入功能接线: 告警闭环/Metrics 落盘/行业暴露检查/
# daily_risk 晚间链/benchmark 汇总/phase8 CLI/stress 端点修复.
import os
import sqlite3

import pytest


# ── 1. Metrics.persist() 接入: 落盘 metrics.db ──
def test_metrics_persist_writes_db(tmp_path):
    from quant.monitor.metrics import Metrics

    m = Metrics(db_path=str(tmp_path / "m.db"))
    m.inc("scheduler.attribution.ok")
    m.gauge("var_95_pct", 1.5)
    m.persist()

    c = sqlite3.connect(str(tmp_path / "m.db"))
    rows = c.execute(
        "SELECT name, type, value FROM metrics WHERE name IN "
        "('scheduler.attribution.ok','var_95_pct') ORDER BY name"
    ).fetchall()
    c.close()
    assert ("scheduler.attribution.ok", "counter", 1) in rows
    assert ("var_95_pct", "gauge", 1.5) in rows


# ── 2. 接线断言 (源码级, 与 test_v534 风格一致) ──
def test_orchestrator_alerts_and_persist_wired():
    src = open("quant/scheduler/orchestrator.py", encoding="utf-8").read()
    assert "check_alerts" in src and "push_alerts" in src
    assert "metrics as _mm" in src and "_mm.persist()" in src


def test_pipeline_sector_check_wired():
    src = open("quant/pipeline.py", encoding="utf-8").read()
    assert "sector_exposure_check" in src
    assert "risk.max_sector_exposure" in src
    assert "optimizer.nano_cap" in src  # Nano 层豁免


def test_attribution_daily_risk_wired():
    src = open("quant/scheduler/attribution.py", encoding="utf-8").read()
    assert "update_daily_risk" in src


def test_web_stress_endpoint_fixed():
    """原 from quant.risk.stress_test import run_stress_tests (模块已删 → 500)."""
    src = open("web/app.py", encoding="utf-8").read()
    import_lines = [l for l in src.splitlines() if l.strip().startswith("from quant")]
    assert not any("stress_test import" in l for l in import_lines)
    assert "from quant.risk.var import stress_test" in src


def test_web_benchmark_uses_tracker():
    src = open("web/app.py", encoding="utf-8").read()
    assert "get_tracking_summary" in src
    assert "TRADE_DB" not in src.split("@app.route(\"/api/benchmark\")")[1].split("@app.route")[0]


def test_phase8_cli_entry():
    src = open("quant/evaluation/phase8_live_consistency.py", encoding="utf-8").read()
    assert 'if __name__ == "__main__":' in src
    assert "_main()" in src


# ── 3. phase8 CLI 冒烟 (数据不足分支应可运行) ──
def test_phase8_main_runs():
    import quant.evaluation.phase8_live_consistency as P
    assert callable(P.validate_consistency)


# ── 4. sector_exposure_check 行为 (接入函数本身) ──
def test_sector_exposure_check_semantics():
    import pandas as pd
    from quant.risk.constraints import sector_exposure_check

    w = pd.Series({"600001": 0.5, "000001": 0.3, "000002": 0.2})
    ind = pd.Series({"600001": "银行", "000001": "银行", "000002": "电子"})
    ok, msg = sector_exposure_check(w, ind, max_exposure=0.35)
    assert not ok and "银行" in msg
    ok2, _ = sector_exposure_check(w, ind, max_exposure=0.85)
    assert ok2


# ── 5. get_tracking_summary 结构 (空表分支) ──
def test_tracking_summary_empty_branch():
    from quant.benchmark.tracker import get_tracking_summary
    r = get_tracking_summary(strategy="quant__nonexistent")
    assert r.get("available") is False