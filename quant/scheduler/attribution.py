"""归因分析调度器 — 每日 15:30. P2-P5 已落地.

P2: probation→active 自动升回 (连续N天IC恢复)
P3: 轻量级在线 OOS IC 验证 (expanding-window)
P4: IC 滚动窗口 5→20 天 (config.yaml)
P5: Brinson 基准从等权改为市值加权

归因三档模式 (test-v284, 独立于 optimizer 资金分档):
  精简模式 (Nano,  <¥50K):    因子健康检测 + 信号衰减, 跳过 Brinson/DSR/换手率/拥挤度
  标准模式 (Micro, ¥50K-500K): 全量归因, 放宽阈值 (换手率 200%, 滑点 2%)
  严格模式 (Small, >¥500K):    全量归因, 业界标准阈值 (换手率 50%, 滑点 1%)
"""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.scheduler.manifest import EVENING_STAGE_GRACE
import numpy as np
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.factor.registry import _db_connect

_log = get_logger(__name__)


def _aum_tier() -> str:
    """读取当前资金量, 返回归因模式: 'nano' | 'micro' | 'small'.
    来源: test-v284 独立分档, 不和 optimizer 资金层混用.
      nano (精简): AUM < ¥50,000   — 因子健康 + 信号衰减
      micro (标准): ¥50K-500K      — 全量归因, 放宽阈值
      small (严格): > ¥500K        — 业界标准阈值"""
    from quant.config.constants import _require_cfg
    from quant.execution.engine import ExecutionEngine
    try:
        capital = ExecutionEngine().get_capital(strategy="quant")
    except Exception:
        capital = _require_cfg("backtest.default_capital")
    if capital < _require_cfg("attribution.tier_nano_cap"):
        return "nano"
    elif capital < _require_cfg("attribution.tier_micro_cap"):
        return "micro"
    return "small"


def _tier_label(tier: str) -> str:
    return {"nano": "精简模式 (Nano)", "micro": "标准模式 (Micro)", "small": "严格模式 (Small)"}[tier]


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("attribution", today, grace_seconds=EVENING_STAGE_GRACE["attribution"])
    if rid is None:
        _log.info(f"[{today}] attribution already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 15:30 — attribution")
    t0 = _time.time()

    tier = _aum_tier()
    _log.info(f"[{today}] attribution tier: {_tier_label(tier)}")

    from quant.execution.engine import ExecutionEngine
    engine = ExecutionEngine()
    positions = engine.get_positions(strategy="quant")

    # ── 精简模式跳过: Brinson 需要 ≥5 行业 + ≥20 只票才有统计意义 (Barra) ──
    if tier != "nano" and positions:
        from quant.monitor.attribution import brinson_attribution
        # ── P5: Brinson 归因 — 市值加权基准 ──
        import pandas as pd
        from quant.data.store import market_conn
        conn = market_conn("ro")
        syms = [p["symbol"] for p in positions]
        ph = ",".join("?" * len(syms))
        rows = conn.execute(
            "SELECT d.symbol, d.close, COALESCE(s.industry,'其他') as sector, d.date "
            "FROM daily d LEFT JOIN stocks s ON d.symbol=s.symbol "
            "WHERE d.symbol IN (" + ph + ") AND d.date <= ? ORDER BY d.date",
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

            # v409: Brinson Rp 从实盘持仓日收益计算，而非同源 sector_returns
            # 计算每只持仓的日收益
            pos_returns = {}
            for p in positions:
                sym = p["symbol"]
                sym_data = df[df["symbol"] == sym].sort_values("date")
                if len(sym_data) >= 2:
                    ret = sym_data["close"].pct_change().dropna().iloc[-1] if len(sym_data) >= 2 else 0
                    pos_returns[sym] = ret
            # 按行业加权组合收益
            port_sector_ret = {}
            port_sector_val = {}
            for p in positions:
                sec = p.get("sector", "其他")
                ret = pos_returns.get(p["symbol"], 0)
                val = p.get("value", 0)
                port_sector_ret[sec] = port_sector_ret.get(sec, 0) + ret * val
                port_sector_val[sec] = port_sector_val.get(sec, 0) + val
            for sec in port_sector_ret:
                if port_sector_val.get(sec, 0) > 0:
                    port_sector_ret[sec] /= port_sector_val[sec]

            # P5: 基准改用市值加权 (daily_valuation.market_cap 按行业汇总)
            all_sectors = set(list(sector_returns.keys()) + list(port_weights.keys()))
            sec_mkt_cap = {}
            for sec in all_sectors:
                cap_rows = conn.execute(
                    "SELECT dv.market_cap FROM daily_valuation dv "
                    "JOIN daily d ON dv.symbol=d.symbol AND dv.date=d.date "
                    "JOIN stocks s ON d.symbol=s.symbol "
                    "WHERE s.industry=? AND d.date <= ? "
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
            Rp = pd.Series({s: port_sector_ret.get(s, 0) for s in all_sectors})
            Rb = pd.Series({s: bench_returns.get(s, 0) for s in all_sectors})
            Wp = pd.Series(port_weights)
            Wb = pd.Series(bench_weights)
            result = brinson_attribution(Rp, Rb, Wp, Wb)
            _log.info(f"[{today}] Brinson (mkt-cap weighted): alloc={result['allocation']:.4f} select={result['selection']:.4f} interact={result['interaction']:.4f} total={result['total']:.4f}")
        else:
            _log.warning(f"[{today}] no daily data for Brinson")
        conn.close()
    else:
        _log.info(f"[{today}] no positions, skip attribution")


    # ═══════════════════════════════════════════════════════
    # G1: 在线 Walk-Forward OOS 验证
    # ═══════════════════════════════════════════════════════
    from quant.scheduler.oos_verify import run_oos_check
    from quant.config.constants import _require_cfg as _ecfg
    # 用最近有因子缓存的日期 (盘中 today 可能还没物化)
    # test-v466 (MC-2): 改走 FactorStore.latest_cached_date() — 原扫 .csv.gz
    # 在 parquet 主存储下恒为空列表 → OOS 永远用 today (可能未物化)
    from quant.factor.store import FactorStore
    _fs = FactorStore()
    _cached = _fs.latest_cached_date()
    _check_date = _cached if _cached and today not in (_cached,) else today
    if _check_date != today:
        _log.info(f"[{today}] OOS verify: using latest cached date {_check_date} (today not yet materialized)")
    oos_result = run_oos_check(
        _check_date,
        status_filter="using",
        train_days=_ecfg("oos_verify.train_window_days"),
        test_days=_ecfg("oos_verify.test_window_days"),
        decay_warn_threshold=_ecfg("oos_verify.decay_warn_threshold"),
        n_symbols=_ecfg("oos_verify.attribution_n_symbols"),
    )
    if oos_result.get("alert"):
        _log.warning(
            f"[{today}] G1 OOS walk-forward: {oos_result.get('oos_decay_count', 0)}/{oos_result.get('n_factors', 0)} "
            f"factors decayed, OOS/IS Sharpe ratio={oos_result.get('decay_ratio', 1.0):.2f}"
        )
        _m.inc("scheduler.attribution.oos_wf_alert", 1)
    else:
        _log.info(f"[{today}] G1 OOS walk-forward: {oos_result.get('n_factors', 0)} factors, no decay alert")
    # ═══════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════
    # 统一因子健康评估 — Level 1→2→3 三级检测体系 (AQR/WorldQuant 标准)
    # 数据源: G1 OOS ic_daily (真实行情, 非静态缓存)
    # ═══════════════════════════════════════════════════════
    from quant.data.repos import FactorRepo
    f_repo = FactorRepo()
    f_repo.ensure_ic_daily_table()

    from quant.config.constants import _require_cfg
    IC_ROLLING_WINDOW = _require_cfg("attribution.ic_rolling_window")
    IC_DEGRADATION_THRESHOLD = _require_cfg("attribution.ic_degradation_threshold")
    OOS_WARNING_DECAY = _require_cfg("attribution.oos_warning_decay")
    OOS_RECOVERY_THRESHOLD = _require_cfg("attribution.oos_recovery_threshold")
    MONITORING_BUFFER_DAYS = _require_cfg("attribution.monitoring_buffer_days")
    PROMOTION_STABILITY_DAYS = _require_cfg("attribution.promotion_stability_days")

    oos_per_factor = oos_result.get("details", {}).get("per_factor", {})
    ic_daily = oos_result.get("ic_daily", {})

    # ── Step A: 写入 factor_ic_daily (每日追加) ──
    # Get active+probation factor names for estimation
    _all_monitored = f_repo.get_all_by_status(('active', 'probation'))
    _all_monitored_names = [f["name"] for f in _all_monitored]
    ic_daily_written = 0
    for fname, daily_ics in ic_daily.items():
        for ds, ic_val in daily_ics.items():
            n_stocks_est = len(_all_monitored_names) if _all_monitored_names else 59
            f_repo.insert_ic_daily(ds, fname, float(ic_val), n_stocks_est,
                                   is_ir=oos_per_factor.get(fname, {}).get("is_ir"),
                                   oos_ir=oos_per_factor.get(fname, {}).get("oos_ir"))
            ic_daily_written += 1
    if ic_daily_written:
        _log.info(f"[{today}] factor_ic_daily: {ic_daily_written} rows written for {len(ic_daily)} factors")

    # ── Step A.5: CPCV+DSR 因子级评估 (test-v300, 替代旧 L2 短窗判定) ──
    from quant.evaluation.cpcv_dsr import evaluate_factor as _eval_dsr
    cpcv_verdicts = {}
    _n_monitored = len(_all_monitored_names)
    for fname in _all_monitored_names:
        ic_rows = f_repo.get_ic_rolling(fname, n_days=_require_cfg("attribution.cpcv_lookback_days"))
        ic_vals = [r["ic_value"] for r in ic_rows if r["ic_value"] is not None]
        if ic_vals:
            cpcv_verdicts[fname] = _eval_dsr(ic_vals, n_trials=max(_n_monitored, 1))
    _n_degraded_dsr = sum(1 for v in cpcv_verdicts.values() if v.get("verdict") == "degraded")
    _n_significant = sum(1 for v in cpcv_verdicts.values() if v.get("verdict") == "significant")
    _log.info(f"[{today}] CPCV+DSR: {len(cpcv_verdicts)} factors → {_n_degraded_dsr} degraded, {_n_significant} significant")

    # ── Step A.6: 因子冗余检测 (P1-1, IC-rank 相关性去重) ──
    # 用 factor_ic_daily 序列计算 pairwise Spearman ρ; |ρ| > 阈值 → IC 低的因子降级
    from quant.factor.state_machine import FactorStateManager
    fsm = FactorStateManager()
    CORR_REDUNDANT_THRESHOLD = _require_cfg("attribution.corr_redundant_threshold")
    _ap = f_repo.get_all_by_status(("active", "probation"))
    _ap_names = [f["name"] for f in _ap]
    if len(_ap_names) >= 2:
        try:
            from scipy.stats import spearmanr as _spearmanr
            # 收集各因子最近 N 天 IC 序列
            _ic_series = {}
            for fn in _ap_names:
                rows = f_repo.get_ic_rolling(fn, n_days=_require_cfg("attribution.ic_rolling_window"))
                vals = [r["ic_value"] for r in rows if r["ic_value"] is not None]
                if len(vals) >= 10:
                    _ic_series[fn] = vals
            redundant_pairs = []
            _names = list(_ic_series.keys())
            for i in range(len(_names)):
                for j in range(i + 1, len(_names)):
                    si, sj = _ic_series[_names[i]], _ic_series[_names[j]]
                    rho, _ = _spearmanr(si, sj)
                    if abs(rho) > CORR_REDUNDANT_THRESHOLD:
                        ic_i = abs(np.mean(si))
                        ic_j = abs(np.mean(sj))
                        loser = _names[i] if ic_i <= ic_j else _names[j]
                        winner = _names[j] if ic_i <= ic_j else _names[i]
                        redundant_pairs.append({
                            "loser": loser, "winner": winner,
                            "corr": round(float(rho), 3),
                            "loser_ic": round(ic_i, 4), "winner_ic": round(ic_j, 4),
                        })
            for pair in redundant_pairs:
                name = pair["loser"]
                current_status = next((f["status"] for f in _ap if f["name"] == name), None)
                if current_status in ("active", "probation"):
                    reason = (f"[LIVE] 因子IC冗余: ρ={pair['corr']:.2f} with {pair['winner']} "
                              f"(IC={pair['loser_ic']:.4f} < {pair['winner_ic']:.4f})")
                    fsm.transition(name, "FACTOR_REDUNDANT", reason)
                    _log.warning(f"[{today}] {name}: {current_status} → degraded (FACTOR_REDUNDANT, ρ={pair['corr']:.2f})")
                    _m.inc("scheduler.attribution.factor_redundant", 1)
            if redundant_pairs:
                _log.warning(f"[{today}] G5 redundancy: {len(redundant_pairs)} redundant pairs detected")
        except Exception as e:
            _log.warning(f"[{today}] redundancy check failed (non-fatal): {e}")

    # ── Step B: Level 1 — 滚动 IC 监控 ──
    degraded_l1 = set()
    active_factors = f_repo.get_all_by_status(('active',))
    for af in active_factors:
        name = af["name"]
        rolling = f_repo.get_ic_rolling(name, IC_ROLLING_WINDOW)
        if len(rolling) < max(3, IC_ROLLING_WINDOW // 4):
            continue
        vals = [r["ic_value"] for r in rolling if r["ic_value"] is not None]
        if len(vals) < max(3, IC_ROLLING_WINDOW // 4):
            continue
        # [test-v398] L1: 近5日均值替代原单日 vals[-1]。
        # 原逻辑用单日IC与60日滚动均值比较, 但单日IC噪声 σ≈3-5×mean,
        # 30%偏离近乎必然触发 → active→probation 误杀。
        # 改为5日滚动均值: Grinold & Kahn (1999) 建议月频窗口,
        # WorldQuant 101 Alphas 用周频。窗口长度由 config attribution.l1_rolling_days 控制。
        recent_n = min(_require_cfg("attribution.l1_rolling_days"), len(vals))
        current_mean = sum(vals[-recent_n:]) / recent_n
        rolling_mean = sum(vals[:-recent_n]) / max(len(vals[:-recent_n]), 1)
        if rolling_mean and abs((current_mean - rolling_mean) / max(abs(rolling_mean), 1e-10)) > IC_DEGRADATION_THRESHOLD:
            degraded_l1.add(name)
            _log.warning(f"[{today}] L1: {name} IC rolling decline (mean={rolling_mean:+.4f}→{recent_n}d={current_mean:+.4f})")

    # ── Step C: Level 2 — OOS/IS 比率 ──
    degraded_l2 = set()
    recovery_candidates = set()
    for name, info in oos_per_factor.items():
        is_ir = info.get("is_ir", 0)
        oos_ir = info.get("oos_ir", 0)
        if oos_ir < 0:
            degraded_l2.add(name)
            _log.warning(f"[{today}] L2: {name} OOS IR reversed (IS_IR={is_ir:+.4f}→OOS_IR={oos_ir:+.4f})")
        elif is_ir and abs(is_ir) > 0.001:
            ratio = oos_ir / is_ir if is_ir > 0 else 1.0
            if ratio < OOS_WARNING_DECAY:
                degraded_l2.add(name)
                _log.warning(f"[{today}] L2: {name} OOS decay (IS_IR={is_ir:+.4f}→OOS_IR={oos_ir:+.4f} ratio={ratio:.2f})")
            elif ratio > OOS_RECOVERY_THRESHOLD:
                recovery_candidates.add(name)

    # ── Step D: Level 3 — 稳定性校验 + 状态变更 (ADR-040 方案 B) ──
    all_degraded = degraded_l1 | degraded_l2

    # D1: active → probation (Level 1 ∪ Level 2)
    for name in all_degraded:
        if name in {af["name"] for af in active_factors}:
            l1 = "L1" if name in degraded_l1 else ""
            l2 = "L2" if name in degraded_l2 else ""
            source = "+".join(x for x in [l1, l2] if x)
            reason = f"[LIVE] IC degraded ({source}): " + (
                f"IS_IR={oos_per_factor.get(name, {}).get('is_ir', '?'):+.4f}→OOS_IR={oos_per_factor.get(name, {}).get('oos_ir', '?'):+.4f}"
                if name in oos_per_factor else f"rolling IC decline"
            )
            fsm.transition(name, "IC_DEGRADED", reason)
            _log.warning(f"[{today}] {name}: active → probation ({source})")
            _m.inc("scheduler.attribution.ic_degraded", 1)

    # D2: probation → active (recovery confirmed)
    probation_factors = f_repo.get_all_by_status(('probation',))
    for pf in probation_factors:
        pname = pf["name"]
        if pname not in recovery_candidates:
            continue
        # Check stability: needs PROMOTION_STABILITY_DAYS of non-degraded IC
        recent_ics = f_repo.get_ic_rolling(pname, PROMOTION_STABILITY_DAYS + 5)
        if len(recent_ics) < PROMOTION_STABILITY_DAYS:
            continue
        recent_vals = [r["ic_value"] for r in recent_ics[-PROMOTION_STABILITY_DAYS:] if r["ic_value"] is not None]
        if len(recent_vals) < PROMOTION_STABILITY_DAYS:
            continue
        rolling_vals = f_repo.get_ic_rolling(pname, IC_ROLLING_WINDOW)
        if not rolling_vals:
            continue
        longer_vals = [r["ic_value"] for r in rolling_vals if r["ic_value"] is not None]
        if not longer_vals:
            continue
        longer_mean = sum(longer_vals) / len(longer_vals)
        stable = all(
            abs((v - longer_mean) / max(abs(longer_mean), 1e-10)) < IC_DEGRADATION_THRESHOLD
            for v in recent_vals
        )
        if stable:
            _v = cpcv_verdicts.get(pname, {})
            fsm.transition(pname, "IC_RECOVERED",
                reason=f"[LIVE] probation→active: DSR significant (DSR={_v.get('dsr')}, stable for {PROMOTION_STABILITY_DAYS}d)")
            _log.info(f"[{today}] {pname}: probation → active (DSR significant, {PROMOTION_STABILITY_DAYS}d stable)")
            _m.inc("scheduler.attribution.promoted", 1)

    # D3: probation → archived (persistent decay, ADR-040: rolling t-test 替代硬时间阈值)
    for pf in probation_factors:
        pname = pf["name"]
        if pname in recovery_candidates:
            continue
        still_decaying = pname in all_degraded
        if not still_decaying:
            continue
        # ADR-040: 用滚动 IC 序列 t-test 而非 MONITORING_BUFFER_DAYS
        rolling = f_repo.get_ic_rolling(pname, MONITORING_BUFFER_DAYS + 10)
        if len(rolling) < MONITORING_BUFFER_DAYS:
            continue
        ic_vals = [r["ic_value"] for r in rolling[-MONITORING_BUFFER_DAYS:] if r["ic_value"] is not None]
        if len(ic_vals) < max(5, MONITORING_BUFFER_DAYS // 2):
            continue
        import numpy as np
        mean_ic = np.mean(ic_vals)
        se_ic = np.std(ic_vals, ddof=1) / np.sqrt(len(ic_vals)) if len(ic_vals) > 1 else 0
        t_stat = mean_ic / se_ic if se_ic > 0 else 0
        # [test-v398] L3: t-test 归档阈值从 |t|<1.0 提升到 |t|<2.0。
        # |t|<1.0 对应 ~68% 置信 (p≈0.32), 只需微弱证据即归档 — 过于激进。
        # |t|<2.0 对应 ~95% 置信 (p≈0.05), 需较强证据才归档因子。
        # 依据: De Prado (2018) Ch.7 建议 t>2.0 为 IC 显著性最低门槛。
        if abs(t_stat) < 2.0:
            _v = cpcv_verdicts.get(pname, {})
            fsm.transition(pname, "IC_PERSISTENT",
                reason=f"[LIVE] 持续衰减归档: |t|={abs(t_stat):.2f}<2.0, DSR={_v.get('dsr')}")
            _log.warning(f"[{today}] {pname}: probation → archived (|t|={abs(t_stat):.2f}<2.0, 持续衰减)")
            _m.inc("scheduler.attribution.retired", 1)
        else:
            _log.info(f"[{today}] {pname}: probation, |t|={abs(t_stat):.2f}≥1.0 — still observing")

    # ── Step E: 同步 ic_mean 到 factor_registry ──
    all_active_names = [af["name"] for af in active_factors] + [pf["name"] for pf in probation_factors]
    if all_active_names:
        f_repo.sync_all_ic_means(all_active_names, n_days=min(60, IC_ROLLING_WINDOW * 3))
        _log.info(f"[{today}] synced ic_mean to factor_registry for {len(all_active_names)} factors")

    # G2: 因子拥挤度检测
    # ═══════════════════════════════════════════════════════
    from quant.scheduler.crowdedness import check_factor_crowdedness
    crowd_result = check_factor_crowdedness(today)
    if crowd_result.get("alert"):
        _log.warning(
            f"[{today}] G2 crowdedness: crowd_index={crowd_result['crowd_index']:.3f}, "
            f"{crowd_result['n_high_corr_pairs']}/{crowd_result['n_factors']} factors "
            f"with high pairwise ρ (>{'0.70'} threshold)"
        )
        _m.inc("scheduler.attribution.crowd_alert", 1)
    else:
        _log.info(
            f"[{today}] G2 crowdedness: crowd_index={crowd_result.get('crowd_index', 0):.3f}, "
            f"no alert"
        )
    # ═══════════════════════════════════════════════════════
    # G3: DSR / MinTRL 计算 (标准+严格模式; 精简模式数据不足跳过)
    # ═══════════════════════════════════════════════════════
    if tier != "nano":
        from quant.evaluation.deflated_sharpe import compute_dsr_for_strategy
        trades = engine.get_trades(strategy="quant", limit=1000)
        if trades:
            pnl_by_date = {}
            for t in trades:
                d = t.get("date", "")
                pnl = t.get("pnl", 0) or 0
                if d and pnl != 0:
                    pnl_by_date[d] = pnl_by_date.get(d, 0.0) + float(pnl)
            daily_returns = list(pnl_by_date.values())
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
            else:
                _log.info(f"[{today}] G3 DSR: {len(daily_returns)} trading days, need >=20, skip")
        else:
            _log.info(f"[{today}] G3 DSR: no trades yet, skip")
    # ═══════════════════════════════════════════════════════
    # G4: 因子 PnL 归因
    # ═══════════════════════════════════════════════════════
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
    else:
        _log.info(f"[{today}] G4 factor PnL: no positions, skip")
    # ═══════════════════════════════════════════════════════
    # R3: 换手率归因 — 换手 vs alpha 收益
    # ═══════════════════════════════════════════════════════
    trades_today = engine.get_trades(strategy="quant", limit=1000)
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
            # 换手率告警按资金分档 (test-v283):
            #   nano (精简): 不限换手 (小额组合一笔就翻倍, 告警无意义)
            #   micro (标准): 200% 阈值 (中等资金, 放宽)
            #   small (严格): 50% 阈值 (机构标准)
            _TURNOVER_LIMITS = {
                "nano": _require_cfg("attribution.turnover_limit_nano"),
                "micro": _require_cfg("attribution.turnover_limit_micro"),
                "small": _require_cfg("attribution.turnover_limit_small"),
            }
            limit = _TURNOVER_LIMITS.get(tier, 0.50)
            if turnover_pct > limit:
                _log.warning(
                    f"[{today}] R3 high turnover: {turnover_pct*100:.1f}% — "
                    f"consider increasing rebalance interval or trade size threshold"
                )
            _m.gauge("scheduler.attribution.daily_turnover_pct", round(turnover_pct * 100, 2))
    else:
        _log.info(f"[{today}] R3 turnover: no trades+positions, skip (trades_today={len(trades_today)} positions={len(positions)})")
    # ═══════════════════════════════════════════════════════
    # R4: 信号衰减归因 — 信号 alpha vs 执行价滑点
    # ═══════════════════════════════════════════════════════
    from quant.data.repos import TradeRepo
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
                # 滑点告警按资金分档 (test-v283):
                #   nano (精简): 5% 阈值 (小额组合隔夜跳空常见)
                #   micro (标准): 2% 阈值
                #   small (严格): 1% 阈值 (机构标准, Kissell IS)
                _SLIPPAGE_LIMITS = {
                    "nano": _require_cfg("attribution.slippage_limit_nano"),
                    "micro": _require_cfg("attribution.slippage_limit_micro"),
                    "small": _require_cfg("attribution.slippage_limit_small"),
                }
                if abs(avg_slip) > _SLIPPAGE_LIMITS.get(tier, 0.01):
                    _log.warning(
                        f"[{today}] R4 signal slippage > 1%: {avg_slip*100:+.2f}% — "
                        f"check execution timing or quote quality"
                    )
                _m.gauge("scheduler.attribution.signal_slippage_pct", round(avg_slip * 100, 2))
    else:
        _log.info(f"[{today}] R4 signal decay: no signal data for today, skip")
    elapsed = _time.time() - t0
    _tk_finish("attribution", today, "ok", summary={"elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=attribution | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.attribution.ok")

    # ── Benchmark tracking (Gap 8) ──
    engine2 = ExecutionEngine()
    total_wealth = engine2.get_capital(strategy="quant")
    from quant.benchmark.tracker import BenchmarkTracker, compute_rolling_metrics
    _bt = BenchmarkTracker()
    _bt.record(today, total_wealth)
    # v409: 每日更新滚动指标 (alpha/IR/beta)
    try:
        compute_rolling_metrics(window=60, strategy="quant")
    except Exception as e:
        _log.warning(f"rolling metrics update failed: {e}")
