"""归因分析调度器 — 每日 15:30."""
import time as _time, uuid as _uuid
from datetime import time
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop

_log = get_logger("quant.scheduler.attribution")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 15:30 — attribution")
    t0 = _time.time()

    from monitor.attribution import brinson_attribution
    from execution.engine import ExecutionEngine
    engine = ExecutionEngine()
    positions = engine.get_positions(strategy="quant")

    if positions:
        _log.info(f"[{today}] Brinson 归因数据准备未实现, 跳过")
    else:
        _log.info(f"[{today}] no positions, skip attribution")

    # ── IC 衰减快照 ──
    try:
        from web.state_broker import broker
        import sqlite3, json
        conn = sqlite3.connect("data/market.db")
        rows = conn.execute(
            "SELECT name, ic_mean FROM factor_registry WHERE status IN ('active','monitoring') AND ic_mean IS NOT NULL"
        ).fetchall()
        conn.close()
        if rows:
            today_weights = {r[0]: round(r[1], 6) for r in rows}
            prev_raw = (broker.get().get("metrics") or {}).get("factor_ic_snapshot")
            prev_weights = json.loads(prev_raw) if prev_raw else {}
            degraded = []
            for name, w in today_weights.items():
                pw = prev_weights.get(name)
                if pw and pw != 0 and abs((w - pw) / pw) > 0.3:
                    degraded.append(f"{name}: {pw:+.4f}→{w:+.4f}")
            if degraded:
                _log.warning(f"[{today}] IC degradation detected: {'; '.join(degraded)}")
                _m.inc("scheduler.attribution.ic_degraded", len(degraded))
                # Auto-degrade: IC 衰减 >30% → monitoring
                for entry in degraded:
                    fname = entry.split(":")[0]
                    try:
                        import sqlite3
                        dc = sqlite3.connect("data/market.db")
                        dc.execute(
                            "UPDATE factor_registry SET status='monitoring', status_reason=? WHERE name=? AND status='active'",
                            (f"IC degraded: {entry}", fname)
                        )
                        dc.commit()
                        dc.close()
                    except Exception:
                        pass
            # monitoring → retired: 已经告警中，持续衰减 → 退役回回测池
            try:
                import sqlite3
                dc2 = sqlite3.connect("data/market.db")
                monitoring_rows = dc2.execute(
                    "SELECT name FROM factor_registry WHERE status='monitoring'"
                ).fetchall()
                for mr in monitoring_rows:
                    mname = mr[0]
                    # 检查是否仍然在今日衰减列表中
                    still_decaying = any(e.startswith(mname + ':') for e in degraded)
                    if still_decaying:
                        dc2.execute(
                            "UPDATE factor_registry SET status='retired', status_reason=? WHERE name=? AND status='monitoring'",
                            (f"持续衰减退役: {next(e for e in degraded if e.startswith(mname + ':'))}", mname)
                        )
                        _log.warning(f'[{today}] {mname}: monitoring → retired (持续IC衰减)')
                        _m.inc("scheduler.attribution.retired", 1)
                dc2.commit()
                dc2.close()
            except Exception:
                pass
            broker.update({"metrics": {"factor_ic_snapshot": json.dumps(today_weights)}})
    except Exception as e:
        _log.warning(f"[{today}] IC snapshot failed (non-fatal): {e}")

    elapsed = _time.time() - t0
    _log.info(f"[SCHEDULER] {today} | TASK=attribution | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.attribution.ok")


def _loop():
    _timed_loop("attribution", time(15, 30), _run, skip_deadline=time(15, 45))
