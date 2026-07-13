"""归因分析调度器 — 每日 15:30."""
import time as _time, uuid as _uuid
from datetime import time
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop
from factor.registry import _db_connect

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
        # ── 构建 Brinson 归因输入 ──
        try:
            import pandas as pd
            from data.store import market_conn
            conn = market_conn("ro")
            syms = [p["symbol"] for p in positions]
            ph = ",".join("?" * len(syms))
            rows = conn.execute(
                "SELECT symbol, close, sector, date FROM daily WHERE symbol IN (" + ph + ") AND date <= ? ORDER BY date",
                syms + [today]
            ).fetchall()
            conn.close()
            if rows:
                df = pd.DataFrame(rows, columns=["symbol", "close", "sector", "date"])
                df["sector"] = df["sector"].fillna("其他")
                # 按行业分组计算行业收益
                sectors = df.groupby("sector")
                sector_returns = {}
                for s, g in sectors:
                    g_sorted = g.sort_values("date")
                    if len(g_sorted) >= 2:
                        ret = g_sorted["close"].pct_change().dropna().mean()
                        sector_returns[s] = ret
                # 组合行业权重
                port_values = {}
                for p in positions:
                    sec = p.get("sector", "其他")
                    port_values[sec] = port_values.get(sec, 0) + p.get("value", 0)
                total_v = sum(port_values.values()) or 1
                port_weights = {k: v/total_v for k, v in port_values.items()}
                # 基准用等权
                all_secs = list(set(list(sector_returns.keys()) + list(port_weights.keys())))
                bench_weights = {s: 1/len(all_secs) for s in all_secs}
                bench_returns = {s: sector_returns.get(s, 0) for s in all_secs}
                for s in port_weights:
                    if s not in bench_returns:
                        bench_returns[s] = 0
                for s in bench_returns:
                    if s not in port_weights:
                        port_weights[s] = 0

                import pandas as pd
                Rp = pd.Series({s: sector_returns.get(s, 0) for s in all_secs})
                Rb = pd.Series({s: bench_returns.get(s, 0) for s in all_secs})
                Wp = pd.Series(port_weights)
                Wb = pd.Series(bench_weights)
                result = brinson_attribution(Rp, Rb, Wp, Wb)
                _log.info(f"[{today}] Brinson: alloc={result['allocation']:.4f} select={result['selection']:.4f} interact={result['interaction']:.4f} total={result['total']:.4f}")
            else:
                _log.warning(f"[{today}] no daily data for Brinson")
        except Exception as e:
            _log.warning(f"[{today}] Brinson 归因失败 (non-fatal): {e}")
    else:
        _log.info(f"[{today}] no positions, skip attribution")

    # ── IC 衰减快照 ──
    try:
        from web.state_broker import broker
        import json
        from config.constants import _market_db_path, _require_cfg
        conn = _db_connect()
        rows = conn.execute(
            "SELECT name, ic_mean FROM factor_registry WHERE status IN ('active','monitoring') AND ic_mean IS NOT NULL"
        ).fetchall()
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
                # Auto-degrade: IC 衰减 >30% → monitoring (same conn, no race)
                for entry in degraded:
                    fname = entry.split(":")[0]
                    try:
                        conn.execute(
                            "UPDATE factor_registry SET status='monitoring', status_reason=? WHERE name=? AND status='active'",
                            (f"IC degraded: {entry}", fname)
                        )
                        _log.warning(f"[{today}] {fname}: active → monitoring (IC degraded)")
                    except Exception:
                        _log.warning(f"[{today}] IC degrade update failed for {fname}", exc_info=True)
                        raise
                # monitoring → retired: 已经告警中，持续衰减 → 退役回回测池
                try:
                    monitoring_rows = conn.execute(
                        "SELECT name FROM factor_registry WHERE status='monitoring'"
                    ).fetchall()
                    for mr in monitoring_rows:
                        mname = mr[0]
                        still_decaying = any(e.startswith(mname + ':') for e in degraded)
                        if still_decaying:
                            conn.execute(
                                "UPDATE factor_registry SET status='retired', status_reason=? WHERE name=? AND status='monitoring'",
                                (f"持续衰减退役: {next(e for e in degraded if e.startswith(mname + ':'))}", mname)
                            )
                            _log.warning(f'[{today}] {mname}: monitoring → retired (持续IC衰减)')
                            _m.inc("scheduler.attribution.retired", 1)
                except Exception:
                    _log.warning(f"[{today}] retired transition check failed", exc_info=True)
                    raise
                conn.commit()
        conn.close()
        if rows:
            broker.update({"metrics": {"factor_ic_snapshot": json.dumps(today_weights)}})
    except Exception as e:
        _log.warning(f"[{today}] IC snapshot failed (non-fatal): {e}")
        raise

    elapsed = _time.time() - t0
    _log.info(f"[SCHEDULER] {today} | TASK=attribution | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.attribution.ok")

    # ── Benchmark tracking (Gap 8) ──
    try:
        engine2 = ExecutionEngine()
        total_wealth = engine2.get_capital(strategy="quant")
        from benchmark.tracker import BenchmarkTracker
        _bt = BenchmarkTracker()
        _bt.record(today, total_wealth)
    except Exception as e:
        _log.warning(f"[{today}] benchmark tracking failed (non-fatal): {e}")


def _loop():
    _timed_loop("attribution", time(15, 30), _run, skip_deadline=time(15, 45))
