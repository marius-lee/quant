"""归因分析调度器 — 每日 15:30. P2-P5 已落地.

P2: monitoring→active 自动升回 (连续N天IC恢复)
P3: 轻量级在线 OOS IC 验证 (expanding-window)
P4: IC 滚动窗口 5→20 天 (config.yaml)
P5: Brinson 基准从等权改为市值加权
"""
import time as _time, uuid as _uuid
import numpy as np
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.scheduler._base import _timed_loop
from quant.factor.registry import _db_connect

_log = get_logger("quant.scheduler.attribution")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 15:30 — attribution")
    t0 = _time.time()

    from quant.monitor.attribution import brinson_attribution
    from quant.execution.engine import ExecutionEngine
    engine = ExecutionEngine()
    positions = engine.get_positions(strategy="quant")

    if positions:
        # ── P5: Brinson 归因 — 市值加权基准 ──
        try:
            import pandas as pd
            from quant.data.store import market_conn
            conn = market_conn("ro")
            syms = [p["symbol"] for p in positions]
            ph = ",".join("?" * len(syms))
            rows = conn.execute(
                "SELECT symbol, close, sector, date FROM daily WHERE symbol IN (" + ph + ") AND date <= ? ORDER BY date",
                syms + [today]
            ).fetchall()
            if rows:
                df = pd.DataFrame(rows, columns=["symbol", "close", "sector", "date"])
                df["sector"] = df["sector"].fillna("其他")
                sectors = df.groupby("sector")
                sector_returns = {}
                for s, g in sectors:
                    g_sorted = g.sort_values("date")
                    if len(g_sorted) >= 2:
                        ret = g_sorted["close"].pct_change().dropna().mean()
                        sector_returns[s] = ret

                port_values = {}
                for p in positions:
                    sec = p.get("sector", "其他")
                    port_values[sec] = port_values.get(sec, 0) + p.get("value", 0)
                total_v = sum(port_values.values()) or 1
                port_weights = {k: v/total_v for k, v in port_values.items()}

                # P5: 基准改用市值加权 (daily_valuation.market_cap 按行业汇总)
                all_sectors = set(list(sector_returns.keys()) + list(port_weights.keys()))
                sec_mkt_cap = {}
                for sec in all_sectors:
                    cap_rows = conn.execute(
                        "SELECT dv.market_cap FROM daily_valuation dv "
                        "JOIN daily d ON dv.symbol=d.symbol AND dv.date=d.date "
                        "WHERE d.sector=? AND d.date <= ? "
                        "ORDER BY d.date DESC LIMIT 1",
                        (sec, today)
                    ).fetchall()
                    sec_mkt_cap[sec] = sum(r[0] for r in cap_rows if r[0]) or 0
                total_mkt = sum(sec_mkt_cap.values()) or 1
                bench_weights = {s: sec_mkt_cap.get(s, 0)/total_mkt for s in all_sectors}
                if all(w == 0 for w in bench_weights.values()):
                    bench_weights = {s: 1/len(all_sectors) for s in all_sectors}

                bench_returns = {s: sector_returns.get(s, 0) for s in all_sectors}
                for s in port_weights:
                    if s not in bench_returns:
                        bench_returns[s] = 0
                for s in bench_returns:
                    if s not in port_weights:
                        port_weights[s] = 0

                import pandas as pd
                Rp = pd.Series({s: sector_returns.get(s, 0) for s in all_sectors})
                Rb = pd.Series({s: bench_returns.get(s, 0) for s in all_sectors})
                Wp = pd.Series(port_weights)
                Wb = pd.Series(bench_weights)
                result = brinson_attribution(Rp, Rb, Wp, Wb)
                _log.info(f"[{today}] Brinson (mkt-cap weighted): alloc={result['allocation']:.4f} select={result['selection']:.4f} interact={result['interaction']:.4f} total={result['total']:.4f}")
            else:
                _log.warning(f"[{today}] no daily data for Brinson")
        except Exception as e:
            _log.warning(f"[{today}] Brinson 归因失败 (non-fatal): {e}")
    else:
        _log.info(f"[{today}] no positions, skip attribution")


    # ═══════════════════════════════════════════════════════
    # IC 衰减检测 + 自动升回 + OOS 验证 (P2+P3+P4)
    # ═══════════════════════════════════════════════════════
    try:
        import json
        from quant.config.constants import _market_db_path, _require_cfg
        from quant.data.repos import FactorRepo
        repo = FactorRepo()
        rows = repo.get_factors_with_ic(('active', 'monitoring'))
        rows = [(r["name"], r["ic_mean"]) for r in rows]
        if rows:
            IC_ROLLING_WINDOW = _require_cfg("attribution.ic_rolling_window")
            IC_DEGRADATION_THRESHOLD = _require_cfg("attribution.ic_degradation_threshold")
            OOS_WARN_THRESHOLD = _require_cfg("attribution.oos_warn_threshold")
            MONITORING_BUFFER_DAYS = _require_cfg("attribution.monitoring_buffer_days")
            PROMOTION_STABILITY_DAYS = _require_cfg("attribution.promotion_stability_days")

            today_weights = {r[0]: round(r[1], 6) for r in rows}
            repo.save_ic_snapshot(today, json.dumps(today_weights))

            # Step 1: 滚动窗口 IC 均值 (P4: 窗口从5→20天)
            recent_snapshots = repo.get_recent_ic_snapshots(n_days=IC_ROLLING_WINDOW)
            rolling_means = {}
            for name in today_weights:
                values = []
                for snap_date, snap in recent_snapshots.items():
                    v = snap.get(name)
                    if v is not None:
                        values.append(v)
                if len(values) >= max(3, IC_ROLLING_WINDOW // 4):
                    rolling_means[name] = sum(values) / len(values)

            # Step 2: IC 衰减检测 (active/monitoring)
            degraded = []
            promoted = []

            for name, w in today_weights.items():
                rm = rolling_means.get(name)
                if rm and rm != 0 and abs((w - rm) / rm) > IC_DEGRADATION_THRESHOLD:
                    degraded.append(f"{name}: mean={rm:+.4f}→{w:+.4f}")

            # P2: 因子自动升回检查 (monitoring→active)
            try:
                monitoring_factors = repo.get_factors_by_status(('monitoring',), [r[0] for r in rows])
                for mf in monitoring_factors:
                    mname = mf["name"]
                    if mname in today_weights:
                        still_degrading = any(e.startswith(mname + ':') for e in degraded)
                        if not still_degrading:
                            rolling_mean = rolling_means.get(mname)
                            current_ic = today_weights.get(mname)
                            if rolling_mean and current_ic and rolling_mean != 0:
                                stability = abs((current_ic - rolling_mean) / max(abs(rolling_mean), 1e-10))
                                if stability < IC_DEGRADATION_THRESHOLD:
                                    # 检查快照历史中连续稳定的天数
                                    stable_days = 0
                                    snap_dates = sorted(recent_snapshots.keys(), reverse=True)
                                    for sd in snap_dates:
                                        sd_ic = recent_snapshots[sd].get(mname)
                                        if sd_ic is not None:
                                            sd_rm = rolling_means.get(mname)
                                            if sd_rm and abs((sd_ic - sd_rm) / max(abs(sd_rm), 1e-10)) < IC_DEGRADATION_THRESHOLD:
                                                stable_days += 1
                                            else:
                                                break
                                        else:
                                            break
                                    if stable_days >= PROMOTION_STABILITY_DAYS:
                                        repo.update_status(mname, 'active',
                                            f"IC recovered: mean={rolling_mean:+.4f}, stable for {stable_days}d")
                                        _log.info(f"[{today}] {mname}: monitoring → active (IC recovered, {stable_days}d stable)")
                                        _m.inc("scheduler.attribution.promoted", 1)
                                        promoted.append(mname)
            except Exception as e:
                _log.warning(f"[{today}] promotion check failed (non-fatal): {type(e).__name__}: {e}")

            if degraded:
                _log.warning(f"[{today}] IC degradation detected ({IC_ROLLING_WINDOW}d rolling): {'; '.join(degraded)}")
                _m.inc("scheduler.attribution.ic_degraded", len(degraded))
                for entry in degraded:
                    fname = entry.split(":")[0]
                    if fname in promoted:
                        continue
                    try:
                        repo.update_status(fname, 'monitoring', f"IC degraded ({IC_ROLLING_WINDOW}d): {entry}")
                        _log.warning(f"[{today}] {fname}: active → monitoring (IC degraded)")
                    except Exception as e:
                        _log.warning(f"[{today}] IC degrade update failed for {fname}: {type(e).__name__}", exc_info=True)

                from datetime import datetime as _dt, timedelta as _td
                _buffer_cutoff = (_dt.now() - _td(days=MONITORING_BUFFER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    monitoring_rows = repo.get_factors_by_status(('monitoring',), [])
                    for mr in monitoring_rows:
                        mname = mr["name"]
                        if mname in promoted:
                            continue
                        still_decaying = any(e.startswith(mname + ':') for e in degraded)
                        if not still_decaying:
                            continue
                        updated_at = repo.get_factor_updated_at(mname)
                        if updated_at and updated_at < _buffer_cutoff:
                            repo.update_status(mname, 'retired',
                                f"持续衰减退役: {next(e for e in degraded if e.startswith(mname + ':'))}")
                            _log.warning(f'[{today}] {mname}: monitoring → retired '
                                         f'(持续IC衰减, 已监控≥{MONITORING_BUFFER_DAYS}d)')
                            _m.inc("scheduler.attribution.retired", 1)
                        else:
                            _log.info(f"[{today}] {mname}: monitoring, still decaying but within {MONITORING_BUFFER_DAYS}d buffer - observing")
                except Exception as e:
                    _log.warning(f"[{today}] retired transition check failed: {type(e).__name__}", exc_info=True)

            # Step 4: P3 轻量级在线 OOS IC 验证
            try:
                oos_alerts = []
                for name in today_weights:
                    if name in promoted:
                        continue
                    rm = rolling_means.get(name)
                    if rm and rm != 0:
                        oos_ratio = max(0.0, today_weights.get(name, 0) / rm) if rm != 0 else 1.0
                        if oos_ratio < OOS_WARN_THRESHOLD:
                            oos_alerts.append(f"{name}: OOS_IC={today_weights[name]:+.4f} vs IS={rm:+.4f} (ratio={oos_ratio:.2f})")
                if oos_alerts:
                    _log.warning(f"[{today}] OOS IC warning (expanding-window): {'; '.join(oos_alerts)}")
                    _m.inc("scheduler.attribution.oos_warning", len(oos_alerts))
            except Exception as e:
                _log.warning(f"[{today}] OOS check failed (non-fatal): {type(e).__name__}: {e}")

            repo.delete_old_ic_snapshots(keep_days=_require_cfg("attribution.snapshot_keep_days"))
    except Exception as e:
        _log.warning(f"[{today}] IC snapshot failed (non-fatal): {type(e).__name__}: {e}")

        pass

    # ═══════════════════════════════════════════════════════
    # G1: 在线 Walk-Forward OOS 验证
    # ═══════════════════════════════════════════════════════
    try:
        from quant.scheduler.oos_verify import run_oos_check
        oos_result = run_oos_check(today)
        if oos_result.get("alert"):
            _log.warning(
                f"[{today}] G1 OOS walk-forward: {oos_result.get('oos_decay_count', 0)}/{oos_result.get('n_factors', 0)} "
                f"factors decayed, OOS/IS Sharpe ratio={oos_result.get('decay_ratio', 1.0):.2f}"
            )
            _m.inc("scheduler.attribution.oos_wf_alert", 1)
        else:
            _log.info(f"[{today}] G1 OOS walk-forward: {oos_result.get('n_factors', 0)} factors, no decay alert")
    except Exception as e:
        _log.warning(f"[{today}] G1 OOS walk-forward failed (non-fatal): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════
    # G3: DSR / MinTRL 计算
    # ═══════════════════════════════════════════════════════
    try:
        from quant.evaluation.deflated_sharpe import compute_dsr_for_strategy
        trades = engine.get_trades(strategy="quant", limit=500)
        if trades:
            daily_returns = []
            for t in trades:
                pnl = t.get("pnl", 0) or 0
                if pnl != 0:
                    daily_returns.append(float(pnl))
            if len(daily_returns) >= 20:
                from quant.data.repos import FactorRepo
                n_active = len(FactorRepo().get_factors_by_status(('active',), []))
                dsr_result = compute_dsr_for_strategy(daily_returns, n_factors=max(n_active, 1),
                                                       skewness=-0.5, kurtosis=8.0)
                _log.info(
                    f"[{today}] G3 DSR: SR(ann)={dsr_result['annualized_sr']:.3f}, "
                    f"DSR={dsr_result['dsr']:.3f}, MinTRL={dsr_result['min_trl_years']:.1f}y, "
                    f"significant={dsr_result['is_significant']}, n_obs={dsr_result['n_obs']}"
                )
                _m.gauge("scheduler.attribution.dsr", dsr_result["dsr"])
    except Exception as e:
        _log.warning(f"[{today}] G3 DSR/MinTRL failed (non-fatal): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════
    # G4: 因子 PnL 归因
    # ═══════════════════════════════════════════════════════
    try:
        if positions:
            from quant.monitor.factor_attribution import factor_pnl_attribution
            factor_attr = factor_pnl_attribution(positions, today)
            if factor_attr:
                top_contributors = sorted(factor_attr.items(),
                                         key=lambda x: abs(x[1].get("contribution_bps", 0)),
                                         reverse=True)[:5]
                summaries = []
                for fname, info in top_contributors:
                    summaries.append(f"{fname}: {info['contribution_bps']:+.1f}bps ({info['direction']})")
                _log.info(f"[{today}] G4 factor PnL: {len(factor_attr)} factors, top: {'; '.join(summaries)}")
                _m.gauge("scheduler.attribution.factor_pnl_factors", len(factor_attr))
    except Exception as e:
        _log.warning(f"[{today}] G4 factor PnL attribution failed (non-fatal): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════
    # R3: 换手率归因 — 换手 vs alpha 收益
    # ═══════════════════════════════════════════════════════
    try:
        trades_today = engine.get_trades(strategy="quant", limit=500)
        trades_today = [t for t in trades_today if t.get("date") == today]
        if trades_today and positions:
            daily_turnover = sum(abs(t.get("price", 0) * t.get("shares", 0)) for t in trades_today)
            daily_pnl = sum(t.get("pnl", 0) or 0 for t in trades_today)
            port_value = sum(p.get("shares", 0) * p.get("price", 0) for p in positions)
            if port_value > 0 and daily_turnover > 0:
                turnover_pct = daily_turnover / port_value
                pnl_bps = daily_pnl / max(port_value, 1) * 10000
                # PnL per turnover: how much alpha per unit of turnover
                efficiency = pnl_bps / max(turnover_pct * 100, 0.01)
                _log.info(
                    f"[{today}] R3 turnover: {turnover_pct*100:.1f}% turnover, "
                    f"PnL={daily_pnl:+.2f} ({pnl_bps:+.1f}bps), "
                    f"efficiency={efficiency:+.2f} bps/1% turnover"
                )
                if turnover_pct > 0.50:
                    _log.warning(
                        f"[{today}] R3 high turnover: {turnover_pct*100:.1f}% — "
                        f"consider increasing rebalance interval or trade size threshold"
                    )
                _m.gauge("scheduler.attribution.daily_turnover_pct", round(turnover_pct * 100, 2))
    except Exception as e:
        _log.warning(f"[{today}] R3 turnover attribution failed (non-fatal): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════
    # R4: 信号衰减归因 — 信号 alpha vs 执行价滑点
    # ═══════════════════════════════════════════════════════
    try:
        from quant.data.trade_repo import TradeRepo
        sig_data = TradeRepo().get_latest_signals()
        if sig_data and sig_data.get("date") == today:
            targets_by_sym = {t["symbol"]: t for t in sig_data.get("targets", [])}
            executed = [t for t in trades_today if t.get("side") == "buy"]
            if executed and targets_by_sym:
                slippages = []
                for t in executed:
                    sym = t["symbol"]
                    if sym in targets_by_sym:
                        signal_price = targets_by_sym[sym].get("price", 0)
                        exec_price = t.get("price", 0)
                        if signal_price > 0 and exec_price > 0:
                            slip_pct = (exec_price / signal_price - 1)
                            slippages.append(slip_pct)
                if slippages:
                    avg_slip = float(np.mean(slippages))
                    _log.info(
                        f"[{today}] R4 signal decay: avg execution slip {avg_slip*100:+.2f}% "
                        f"across {len(slippages)} buys (signal→execution price)"
                    )
                    if abs(avg_slip) > 0.01:
                        _log.warning(
                            f"[{today}] R4 signal slippage > 1%: {avg_slip*100:+.2f}% — "
                            f"check execution timing or quote quality"
                        )
                    _m.gauge("scheduler.attribution.signal_slippage_pct", round(avg_slip * 100, 2))
    except Exception as e:
        _log.warning(f"[{today}] R4 signal decay attribution failed (non-fatal): {type(e).__name__}: {e}")

    elapsed = _time.time() - t0
    _log.info(f"[SCHEDULER] {today} | TASK=attribution | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.attribution.ok")

    # ── Benchmark tracking (Gap 8) ──
    try:
        engine2 = ExecutionEngine()
        total_wealth = engine2.get_capital(strategy="quant")
        from quant.benchmark.tracker import BenchmarkTracker
        _bt = BenchmarkTracker()
        _bt.record(today, total_wealth)
    except Exception as e:
        _log.warning(f"[{today}] benchmark tracking failed (non-fatal): {e}")


def _loop():
    _timed_loop("attribution", time(15, 30), _run, skip_deadline=time(15, 45))
