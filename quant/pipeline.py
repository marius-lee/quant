"""量化选股 Pipeline — 串联 Layer 0-7, 每个交易日盘后自动运行。

每个 Layer 独立 try/except — 单层异常不中断后续层。
"""

import sys
import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from quant.data.store import DataStore

from quant.risk.neutralize import neutralize

from quant.factor.stats_cache import load_ic_map_from_cache, _bayesian_shrink_ic_map
from quant.risk.covariance import covariance_matrix
from quant.risk.constraints import RiskLimits, apply_all_filters
from quant.risk.var import compute_var
from quant.optimizer.portfolio import PortfolioConstructor
from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine
from quant.monitor.report import generate_report, push_to_web
from quant.config.constants import _require_cfg
from quant.config.paths import TRADE_DB
from quant.core.phase_tracker import PhaseTracker, PhaseResult
from quant.utils.logger import get_logger

import uuid as _uuid

logger = get_logger("pipeline")

LOT_SIZE = _require_cfg("backtest.lot_size")



def generate_signals(date_str: str = None, capital: float = None, strategy: str = "quant",
                     skip_pull: bool = False, store=None, status_filter: str = "using",
                     scope: str = "live",
                     suppress_push: bool = False, universe_size: int = None,
                     db_path: str = TRADE_DB, exclude_symbols: list = None, ic_map: dict = None, combine_mode: str = None, preloaded_data=None, primitives: dict = None, factor_store=None,
                     regime_label: str = None, regime_probs: dict = None,
                     factor_cache: dict = None,  # test-v397 (P0): 预加载因子缓存 {date: {factor: Series}}
                     turnover_amount_roll = None,  # test-v398 (perf): 成交额滚动均值 DataFrame, 按日排序
                     bm_returns: "pd.Series | None" = None,  # test-v398 (perf): 预加载 benchmark 收益序列
                     stock_names: dict = None,  # test-v398 (perf): 预加载股票名称 {symbol: name}
                     preloaded_seal_ratios: dict = None,  # test-v398 (perf): 预加载涨停封成比 {date: {symbol: ratio}}
                     prebuilt_engine = None,  # test-v398 (perf): 复用 ExecutionEngine
                     prebuilt_cost_model = None,  # test-v398 (perf): 复用 CostModel
                     prebuilt_constructor = None,  # test-v398 (perf): 复用 PortfolioConstructor
                     fund_stocks_df = None,  # test-v398 (perf): 预加载 stocks 静态表
                     fund_val_piv = None,    # test-v398 (perf): 预加载 daily_valuation pivot
                     fund_close_piv = None,  # test-v398 (perf): 预加载 close pivot (复用 data_full)
                     fund_high_52w = None,   # test-v398 (perf): 预加载 52w high
                     all_symbols: list = None,  # test-v398 (perf): 预加载全量 symbol 列表
                     ctx: "PipelineContext | None" = None) -> dict:
    """Pipeline 阶段一: 盘前信号生成 (Steps 0-5, 不执行交易)。

    用 T-1 收盘数据计算因子 → alpha → 风险过滤 → 组合优化 → 输出目标持仓。
    status_filter: 控制因子计算池 ('using'=active+probation, 'backtesting'=evaluating+probation)
    scope: 控制 IC 权重来源 ('live'=factor_registry, 'backtest'=factor_ic_daily)
    ic_map: 显式传入 IC 权重 (回测用), 传入后跳过 scope 参数
    regime_label/regime_probs: §8.2 regime 条件合成 (test-v299)。
        实盘 (scope=live) 缺省自动调 get_current_regime() (pickle 缓存 HMM);
        回测 (scope=backtest) 必须由 loop.py 注入 point-in-time 值 —
        自动拉取会用全量历史训练的模型, 构成前视, 故 backtest scope 下不自动取。
    返回: {date, strategy, total_capital, target_positions: [{symbol, shares, price, side}]}
    """
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")

    from quant.utils.logger import get_trace_id, set_trace_id as _set_tid
    tid = get_trace_id() or _uuid.uuid4().hex[:12]
    _set_tid(tid)
    from quant.monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")
    # A3: resolve dependencies from PipelineContext if provided

    if ctx is not None:
        store = store or ctx.store
        factor_store = factor_store or ctx.factor_store
        db_path = db_path or ctx.db_path
        suppress_push = suppress_push or ctx.suppress_push
        if ctx.preloaded_data is not None:
            preloaded_data = preloaded_data or ctx.preloaded_data
        if ctx.primitives:
            primitives = primitives or ctx.primitives
        if ctx.ic_map is not None:
            ic_map = ic_map if ic_map is not None else ctx.ic_map

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    tracker = PhaseTracker("generate_signals")
    import time as _time_ph
    _ph_t0 = _time_ph.time()
    _ph_start = _time_ph.time()
    if not suppress_push:
        from web.state_broker import broker
        broker.update({"status": "signals_started", "progress": "0/5", "date": date_str, "trace_id": tid})
    logger.info(f"generate_signals started trace_id={tid} date={date_str}")

    # ── Step 0: Init ──
    _store_in = store
    store = store or DataStore()  # DataStore 始终用 quant/data/market.db
    # test-v398 (perf): 回测复用预构建实例, 避免每日 new + DDL
    engine = prebuilt_engine or ExecutionEngine(db_path=db_path)
    cost_model = prebuilt_cost_model or CostModel.from_config()
    constructor = prebuilt_constructor or PortfolioConstructor()

    from quant.data.repos import TradeRepo
    seed = TradeRepo(db_path=db_path).get_initial_capital(strategy)
    if not engine.is_initialized(strategy):
        engine.set_initial_capital(strategy, seed)
    # B-12 fix: 显式传入 capital 时按当前权益 sizing (回测 walk-forward 复利);
    # 未传入时 (实盘) 保持原行为用初始本金。
    total_capital = capital if capital else seed
    logger.info(f"[0/5] init: DataStore+Engine ready")

    # ── Step 1: Data Update ──
    if not skip_pull:
        n_new = store.update_daily(start=_require_cfg("data.start_date"))
        results["steps"]["data"] = {"new_rows": n_new, "status": "ok"}
        logger.info(f"[1/5] data: {n_new} new daily rows")
        tracker.phases.append(PhaseResult(name="sync", started=_ph_start, finished=_time_ph.time(), status="ok"))
        _ph_start = _time_ph.time()
        if not suppress_push:
            broker.update({"status": "data_synced", "progress": "1/5", "new_rows": n_new, "trace_id": tid})
        _m.inc("data.sync.rows", n_new)
    else:
        results["steps"]["data"] = {"new_rows": 0, "status": "skipped"}
        logger.info(f"[1/5] data: skipped (skip_pull=True)")

    # ── Step 2: Load ──
    # test-v398 (perf): 回测用预加载 symbol 列表, 跳过每日 SQL JOIN (daily⋈stocks)
    if all_symbols is not None:
        symbols = list(all_symbols)
    else:
        from quant.data.repos import UniverseRepo
        if not universe_size:
            from quant.config.loader import get as _cfg_get
            _ucfg = _cfg_get("universe")
            symbols = UniverseRepo().get_symbols(
                exclude_market="BJ",
                exclude_st=_ucfg["exclude_st"],
                exclude_new_stock_days=_ucfg["exclude_new_stock_days"],
                min_price=_ucfg["min_price"],
                exclude_zero_turnover_days=_ucfg["exclude_zero_turnover_days"],
                min_daily_amount=_ucfg["min_daily_amount"],
            )
        else:
            symbols = UniverseRepo().get_symbols(exclude_market="BJ")

    logger.info(f"[2/5] load: loading {len(symbols)} symbols from DB...")
    from quant.factor.windows import max_factor_calendar_days
    _eff_days = max(_require_cfg("data.lookback_days"), max_factor_calendar_days(None))
    hist_start = (pd.Timestamp(date_str) - pd.Timedelta(days=_eff_days)).strftime("%Y-%m-%d")
    if preloaded_data is None:
        data = store.get_daily(symbols, start=hist_start, end=date_str)
    else:
        data = preloaded_data.loc[:pd.Timestamp(date_str)]
    # test-v398 (perf): 回测从共享 pivot 表组装基本面, 跳过 DB 查询
    # 存 pivot 而非 dict-of-DF: 全量回测 ~400MB, 每日期组装 O(1) 切片
    if fund_stocks_df is not None:
        _ts = pd.Timestamp(date_str)
        # 回退值: stocks 静态列, PIT val_piv 不可用时用这个
        _dyn = {c: fund_stocks_df[c] for c in ("pe_ttm", "pb", "market_cap")
                if c in fund_stocks_df.columns}
        if fund_val_piv is not None and _ts in fund_val_piv.index:
            _row = fund_val_piv.loc[_ts]  # Series with MultiIndex (field, symbol)
            for _col in ["pe_ttm", "pb", "market_cap"]:
                if _col in _row.index.get_level_values(0):
                    _dyn[_col] = _row.loc[_col]  # PIT 覆盖回退值
        if fund_close_piv is not None and _ts in fund_close_piv.index:
            _dyn["close_latest"] = fund_close_piv.loc[_ts]
        if fund_high_52w is not None and _ts in fund_high_52w.index:
            _dyn["high_52w"] = fund_high_52w.loc[_ts]
        fundamentals = pd.DataFrame({
            **{c: fund_stocks_df[c] for c in fund_stocks_df.columns
               if c not in ("pe_ttm", "pb", "market_cap", "close_latest", "high_52w")},
            **_dyn,
        }, index=fund_stocks_df.index)
        # derive ROE
        _null_roe = fundamentals["roe"].isna() | (fundamentals["roe"] <= 0)
        if _null_roe.any():
            _pe_col = "pe_ttm" if "pe_ttm" in fundamentals.columns else "pe"
            _derived = fundamentals["pb"] / fundamentals[_pe_col].replace(0, None)
            _derived = _derived.where((_derived > 0) & (_derived < 100))
            fundamentals.loc[_null_roe, "roe"] = _derived.loc[_null_roe]
        fundamentals.loc[fundamentals["pe"] <= 0, "pe"] = None
        fundamentals.loc[fundamentals["pe"] > 1000, "pe"] = None
        fundamentals.loc[fundamentals["pb"] <= 0, "pb"] = None
        # 过滤到当前 symbols
        fundamentals = fundamentals[fundamentals.index.isin(symbols)]
    else:
        fundamentals = store.get_fundamentals(symbols, date=date_str)
    results["steps"]["load"] = {"symbols": len(symbols), "status": "ok"}
    pe_cnt = int(fundamentals["pe"].notna().sum()) if "pe" in fundamentals.columns else 0
    pb_cnt = int(fundamentals["pb"].notna().sum()) if "pb" in fundamentals.columns else 0
    logger.info(f"[2/5] load: {len(symbols)} symbols, {data.shape[0]} days, PE/PB={pe_cnt}/{pb_cnt}")
    tracker.phases.append(PhaseResult(name="load", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()
    if not suppress_push:
        broker.update({"status": "data_loaded", "progress": "2/5", "symbols": len(symbols), "trace_id": tid})

    # ── Step 2.3: Risk pre-filters (liquidity / price / ST) → investable universe ──
    # Industry standard: risk filters applied to the ENTIRE universe BEFORE alpha scoring.
    # This replaces the old Step 4 apply_all_filters on the alpha-scored subset.
    _risk_limits = RiskLimits.from_config()
    _latest_close = data["close"].iloc[-1].dropna()
    _latest_amount = data["amount"].iloc[-1] if "amount" in data else pd.Series(dtype=float)
    _pre_df = pd.DataFrame({"close": _latest_close, "amount": _latest_amount})
    _pre_filtered = apply_all_filters(_pre_df, limits=_risk_limits, stock_names=stock_names if stock_names else store.get_stock_names(symbols))
    # ── 涨停封死预过滤 (test-v211): 昨日封成比>阈值的股票今日无法交易 ──
    # test-v398 (perf): 回测使用预加载的封成比表, 跳过每日 SQLite 连接
    from quant.risk.constraints import filter_sealed_limit_up
    if preloaded_seal_ratios is not None:
        # Find the most recent date before date_str
        _sorted_dates = sorted(preloaded_seal_ratios.keys())
        _prev_dates = [d for d in _sorted_dates if d < date_str]
        if _prev_dates:
            _pre_filtered = filter_sealed_limit_up(_pre_filtered, _prev_dates[-1],
                                                    seal_ratio_threshold=_require_cfg("universe.sealed_limit_up_ratio"),
                                                    seal_ratios=preloaded_seal_ratios)
    else:
        import sqlite3
        from quant.config.paths import MARKET_DB
        _mconn = sqlite3.connect(MARKET_DB)
        _mconn.execute("CREATE TABLE IF NOT EXISTS limit_up_pool (date TEXT, symbol TEXT, seal_ratio REAL, PRIMARY KEY(date, symbol))")
        _prev_dates = _mconn.execute(
            "SELECT date FROM limit_up_pool WHERE date < ? ORDER BY date DESC LIMIT 1",
            (date_str,)
        ).fetchone()
        _mconn.close()
        if _prev_dates:
            _pre_filtered = filter_sealed_limit_up(_pre_filtered, _prev_dates[0],
                                                    seal_ratio_threshold=_require_cfg("universe.sealed_limit_up_ratio"))
    investable_symbols = _pre_filtered.index.tolist()
    logger.info(f"[2.3] risk pre-filters: {len(symbols)} → {len(investable_symbols)} investable "
                f"(liquidity>{_risk_limits.min_daily_amount}, price>{_risk_limits.min_price}, no ST, limit-up)")
    # Feed investable universe into subsequent steps
    symbols = [s for s in symbols if s in set(investable_symbols)]
    data = data.loc[:, data.columns.get_level_values(1).isin(symbols)]
    fundamentals = fundamentals[fundamentals.index.isin(symbols)]
    results["steps"]["risk_pre"] = {"investable": len(symbols), "status": "ok"}
    logger.info(f"[debug] after Step 2.3: symbols={len(symbols)}")

    # ── Step 2.5: Universe size filter (backtest only) ──
    if universe_size and len(symbols) > universe_size:
        close_df = data["close"]
        latest_date = close_df.index[-1]
        latest_close = close_df.loc[latest_date].dropna()
        candidate_syms = set(latest_close.index)
        if _require_cfg("backtest.universe_filter_affordable"):
            affordable = latest_close[latest_close * LOT_SIZE <= total_capital]
            if len(affordable) > 0:
                candidate_syms &= set(affordable.index)
            # else: empty affordable pool → keep all (edge case for tiny capital)

        # ── Step 2.5b: Rank by turnover, take top N ──
        candidates = list(candidate_syms & set(symbols))
        # test-v398 (perf): 从 _amount_roll 按日排序, O(N log N) ~1ms, 省 ~500MB 内存
        if turnover_amount_roll is not None and date_str in turnover_amount_roll.index:
            _row = turnover_amount_roll.loc[date_str].dropna().sort_values(ascending=False)
            _cand_set = set(candidates)
            keep_syms = [s for s in _row.index if s in _cand_set][:universe_size]
        else:
            keep_syms = store.rank_by_turnover(candidates, date_str,
                            lookback_days=_require_cfg("backtest.universe_turnover_days"),
                            top_n=universe_size)
        symbols = [s for s in symbols if s in keep_syms]
        # data is wide-format MultiIndex columns (field, symbol) — filter 2nd level
        data = data.loc[:, data.columns.get_level_values(1).isin(keep_syms)]
        fundamentals = fundamentals[fundamentals.index.isin(keep_syms)]
        results["steps"]["load"]["symbols"] = len(symbols)
        logger.info(f"[debug] after Step 2.5: symbols={len(symbols)}")


    # ── Step 2.6: Cooling-off exclude (backtest only) ──
    if exclude_symbols:
        symbols = [s for s in symbols if s not in exclude_symbols]
        data = data.loc[:, data.columns.get_level_values(1).isin(symbols)] if symbols else data.iloc[:, []]
        fundamentals = fundamentals[fundamentals.index.isin(symbols)]
    # ── Step 3: Factor + Alpha ──
    actual_date = date_str
    if pd.Timestamp(actual_date) not in data.index:
        actual_date = data.index[-1].strftime("%Y-%m-%d")
        logger.info(f"[3/5] date adjusted: {date_str} -> {actual_date}")

    # test-v398 (perf): benchmark 收益从预加载 Series 切片, 不查 DB
    benchmark_ret = None
    if bm_returns is not None and not bm_returns.empty:
        benchmark_ret = bm_returns[:pd.Timestamp(actual_date)]
    else:
        bm = store.get_benchmark("000300", start=_require_cfg("benchmark.start_date"))
        if not bm.empty:
            benchmark_ret = bm[:pd.Timestamp(actual_date)]

    # ── ztd 预计算缓存: 回测由 loop.py 一次性预加载, 避免每日期 O(n²) 重算 ──
    if scope != "backtest":
        from quant.factor.compute.price._alternative import preload_ztd_cache
        from quant.execution.calendar import is_trading_day as _is_td
        _ztd_dates = [d for d in pd.date_range(start=pd.Timestamp(hist_start), end=pd.Timestamp(date_str), freq="B") if _is_td(d.date())]
        preload_ztd_cache([d.strftime("%Y-%m-%d") for d in _ztd_dates], symbols)

    # ── 因子值来源: factor_cache (内存) 优先, 否则 factor_store (gzip I/O) ──
    # test-v397 (P0): 回测预加载全量因子值到内存, 跳过逐日 gzip 解压
    if factor_cache is not None:
        factor_values = factor_cache.get(actual_date, {})
        if factor_values:
            logger.info("step 3: loaded %d factors from factor_cache (memory) for %s",
                        len(factor_values), actual_date)
    elif factor_store is None:
        raise RuntimeError(
            f"step 3: factor_store is None for {actual_date}, "
            f"run factor_cache materialization first: PYTHONPATH=. .venv/bin/python3 -c "
            f"'from quant.scheduler.factor_cache import _run; _run({actual_date!r}, {actual_date!r})'"
        )
    else:
        factor_values = factor_store.load(actual_date, symbols=symbols, factor_names=None)
        if factor_values:
            logger.info("step 3: loaded %d factors from factor_cache for %s", len(factor_values), actual_date)

    if not len(symbols):
        logger.warning(f"[2.5] no symbols left for date={actual_date}, returning empty signals")
        return {"date": actual_date, "target_positions": [], "signal_count": 0, "steps": results["steps"]}
    if not factor_values:
        raise RuntimeError(
            f"step 3: factor_cache miss for {actual_date} ({len(symbols)} symbols), "
            f"run factor_cache materialization first"
        )
    n_valid = sum(1 for v in factor_values.values() if isinstance(v, pd.Series) and v.notna().sum() > 0)

    from quant.alpha.model import AlphaModel
    # B-12 fix: combine_mode 参数此前被静默忽略 (回测 warmup→ic_weighted 切换无效)
    am = AlphaModel(combine_mode=combine_mode)
    ic_map = ic_map if ic_map is not None else load_ic_map_from_cache(factor_values, scope=scope)

    # v390: probation 因子 IC 衰减 — 状态机降级后权重减半, 不再以全权重交易
    try:
        from quant.data.repos import FactorRepo
        _probation_names = FactorRepo().get_probation_factor_names()
        if _probation_names:
            ic_map = dict(ic_map)  # 不修改传入的原始dict
            for _pn in _probation_names:
                if _pn in ic_map:
                    _v = ic_map[_pn]
                    if isinstance(_v, dict):
                        _v["ic_mean"] = _v.get("ic_mean", 0) * 0.5
                    else:
                        ic_map[_pn] = float(_v) * 0.5
            logger.info("probation decay: %d factors halved: %s", len(_probation_names),
                      ", ".join(sorted(_probation_names)))
    except Exception:
        pass

    # v390: Bayesian收缩仅对 live scope (factor_registry ic_mean), 回测 OOS-IR 已鲁棒
    if scope == "live":
        ic_map = _bayesian_shrink_ic_map(ic_map)

    # ── test-v397 (Problem 3 / P1): 因子层面中性化 (先于合成, 共享投影矩阵) ──
    # Barra USE4 标准: 每个原始因子独立做行业+市值中性化，消除风格偏差。
    # P1 优化: 用 neutralize_factors_batch() 预构建一次投影矩阵 P, 30 因子共享,
    # 避免逐因子 lstsq → ~30x 加速。
    try:
        _ind_info = fundamentals["industry"].reindex(factor_values[next(iter(factor_values))].index) \
            if "industry" in fundamentals.columns else None
        _mcap_info = fundamentals["total_mv"].reindex(factor_values[next(iter(factor_values))].index) \
            if "total_mv" in fundamentals.columns else None
        if _ind_info is not None or _mcap_info is not None:
            from quant.risk.neutralize import neutralize_factors_batch
            factor_values = neutralize_factors_batch(
                factor_values, industries=_ind_info, market_caps=_mcap_info,
            )
            logger.info("step 3: factor-level neutralize applied (%d factors, batch P)", len(factor_values))
    except Exception as _fneut_err:
        logger.warning(f"step 3: factor-level neutralize failed (non-fatal): {_fneut_err}")

    # test-v299 §8.2: regime 条件合成 (HMM 牛/熊/震荡 → 因子权重偏置)
    if _require_cfg("alpha.regime_combine"):
        if regime_label is None and scope == "live":
            from quant.regime.detector import get_current_regime
            regime_label, regime_probs = get_current_regime()
        if regime_label is not None:
            from quant.regime.detector import get_regime_sizing
            _sizing = get_regime_sizing(regime_label)
            if not suppress_push:
                broker.update({"regime": regime_label, "regime_sizing": _sizing,
                           "regime_confidence": round(regime_probs.get(regime_label, 0), 2) if regime_probs else 0})
            alpha_raw = am.combine_regime(factor_values, ic_map=ic_map,
                                          regime_label=regime_label,
                                          regime_probs=regime_probs or {})
        else:
            # backtest scope 未注入 point-in-time regime → 标准合成 (防前视)
            alpha_raw = am.combine(factor_values, ic_map=ic_map)
    else:
        alpha_raw = am.combine(factor_values, ic_map=ic_map)
    alpha = am.rank(alpha_raw)

    # test-v249: 周线多周期信号确认 (MultiTimeframeConfirmer, 压制逆势信号)
    if _require_cfg("alpha.multi_tf_confirm"):
        try:
            from quant.alpha.multi_tf import MultiTimeframeConfirmer
            mtf = MultiTimeframeConfirmer()
            alpha = mtf.confirm(alpha, date_str)
            logger.info(f"step 3: multi_tf confirmation applied")
        except Exception as e:
            logger.warning(f"step 3: multi_tf confirm failed (non-fatal): {e}")

    results["_factor_values"] = {k: v for k, v in factor_values.items() if isinstance(v, pd.Series)}
    results["_alpha_raw"] = alpha_raw
    results["steps"]["factor"] = {"factors": len(factor_values), "valid_stocks": alpha.dropna().count(), "status": "ok"}
    if not suppress_push:
        broker.update({"status": "factors_computed", "progress": "3/5", "n_factors": len(factor_values), "trace_id": tid})
    _m.gauge("factor.n_active", len(factor_values))
    tracker.phases.append(PhaseResult(name="factor", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()

    # ── Step 4: Risk ──
    cov = None  # 协方差矩阵, Step 4 内计算, 供 Step 5 的 construct() 使用
    close_df = data["close"]
    risk_date = actual_date if actual_date in close_df.index else close_df.index[-1].strftime("%Y-%m-%d")
    prices = close_df.loc[risk_date].dropna()
    mcap_real = fundamentals["total_mv"].reindex(prices.index)
    mcap_real = mcap_real.fillna(prices * 1e8)
    industries = fundamentals["industry"].reindex(prices.index) if "industry" in fundamentals.columns else None
    industry_min = _require_cfg("risk.neutralize.min_common_stocks")
    if industries is not None and industries.notna().sum() < industry_min:
        industries = None
    alpha_neut = neutralize(alpha, industries=industries, market_caps=mcap_real)

    # v393: 协方差懒计算 — 仅传 log_ret, Small 层在 construct() 内按需算
    log_ret = np.log(close_df).diff().dropna(how="all")
    cov = None  # construct() 按需懒计算 (test-v398: v393 已实现, cov=None 传递)

    # Step 4 candidates: alpha_neut already within investable universe (pre-filtered in Step 2.3)
    # Risk pre-filters (liquidity/price/ST) are already applied — no re-filtering here.
    # Only stocks with valid alpha scores pass through to the optimizer.
    # Ensure both Series have string indices for DataFrame construction
    alpha_neut.index = alpha_neut.index.astype(str)
    prices.index = prices.index.astype(str)
    candidates = pd.DataFrame({
        "alpha": alpha_neut, "close": prices,
    })
    # Drop rows where alpha is NaN (stocks not scored by alpha model)
    filtered = candidates.dropna(subset=["alpha"])
    results["steps"]["risk"] = {"candidates": len(filtered), "status": "ok"}
    logger.info(f"[4/5] risk: {len(filtered)} candidates after filters")
    tracker.phases.append(PhaseResult(name="risk", started=_ph_start, finished=_time_ph.time(), status="ok"))
    _ph_start = _time_ph.time()

    # test-v397: VaR check relocated to PortfolioConstructor.construct() after cov compute.
    # Old check (cov is None → dead code since v393): kept as no-op for backward compat.
    if cov is not None:
        try:
            _v = candidates["close"].dropna() if "close" in candidates.columns else pd.Series(dtype=float)
            _exposure = float(_v.sum()) * LOT_SIZE if len(_v) > 0 else 0
            if _exposure > 0:
                _w = pd.Series(1.0 / max(len(_v), 1), index=_v.index)
                _var = compute_var(_exposure, _w, cov, confidence=0.95)
                if _var and abs(_var / _exposure) > 0.03:
                    logger.warning("[4/5] VaR warning: daily VaR=%.1f (%.1f%% of exposure)",
                                   abs(_var), abs(_var / _exposure) * 100)
        except Exception as _var_err:
            logger.debug("[4/5] VaR check skipped (non-fatal): %s", _var_err)
    if not suppress_push:
        broker.update({"status": "risk_filtered", "progress": "4/5", "candidates": len(filtered), "trace_id": tid})

    # ── Step 5: Optimizer (generate target positions, do NOT execute) ──
    # §8.3 成本感知换仓 (Grinold α−λ·TC): 当前持仓传入优化器, 效益 < λ×成本
    # 的换股被拦截。engine 的 db_path 回测=BACKTEST_DB / 实盘=TRADE_DB,
    # 两侧同一持仓口径, 行为自动一致。
    _held = engine.get_positions(strategy)
    current_lots = pd.Series(
        {p["symbol"]: int(p["shares"]) // LOT_SIZE for p in _held
         if int(p["shares"]) >= LOT_SIZE},
        dtype=int,
    )
    # ── test-v309 §8.3: regime 动态仓位管理 ──
    # 牛/熊/震荡 → 调整可用资金, 熊市少买保留现金.
    _sizing_capital = total_capital
    if regime_label and regime_label != "unknown":
        from quant.regime.detector import get_regime_sizing
        _multiplier = get_regime_sizing(regime_label)
        _sizing_capital = total_capital * _multiplier
        logger.info(f"[5/5] regime sizing: {regime_label} → "
                    f"capital=¥{_sizing_capital:,.0f} (×{_multiplier})")
    portfolio = constructor.construct(
        filtered["alpha"], filtered["close"],
        _sizing_capital,
        covariance=cov, ic_map=ic_map,
        current_lots=current_lots, cost_model=cost_model,
        log_returns=log_ret,
        regime_label=regime_label,
    )
    if portfolio.tc_suppressed:
        logger.info(f"[5/5] tc_band: {portfolio.tc_suppressed} swap(s) suppressed "
                    f"(held={len(current_lots)} lots)")
    # Build target positions list for the scheduler to consume
    target_positions = []
    for sym, lots in portfolio.lots.items():
        if lots > 0 and sym in prices:
            score = round(float(alpha_neut.get(sym, 0)), 4)
            target_positions.append({
                "symbol": sym,
                "score": score if not (isinstance(score, float) and score != score) else 0.0,
                "shares": int(lots) * LOT_SIZE,
                "price": round(float(prices[sym]), 2),
                "side": "buy",
                "industry": str(industries.get(sym, "")) if (industries is not None and not (isinstance(industries.get(sym, ""), float) and industries.get(sym, "") != industries.get(sym, ""))) else "",
            })
    # ── rank by score descending, annotate factor attribution (test-v206) ──
    target_positions.sort(key=lambda x: x.get("score", 0), reverse=True)
    from quant.alpha.synth import factor_attribution
    target_syms = [tp["symbol"] for tp in target_positions]
    attr_map = factor_attribution(factor_values, target_syms,
                                  positions_per_factor=constructor.positions_per_factor,
                                  max_factors=3)
    for i, tp in enumerate(target_positions):
        tp["reason"] = attr_map.get(tp["symbol"], f"#{i+1}")
    # §8.3 成本带标注: 被拦截而留仓的持仓在 reason 中标记 (进 daily_signals, web 可见)
    if portfolio.tc_suppressed:
        _cur_syms = set(current_lots.index)
        for tp in target_positions:
            if tp["symbol"] in _cur_syms:
                tp["reason"] = "tc_hold(成本带留仓) " + tp.get("reason", "")
    results["target_positions"] = target_positions
    results["steps"]["optimizer"] = {
        "method": portfolio.method, "positions": portfolio.positions,
        "invested": round(portfolio.invested, 2), "status": "ok",
        "tc_suppressed": portfolio.tc_suppressed,
    }
    logger.info(f"[5/5] optimizer: {portfolio.method}, {portfolio.positions} pos, invested=Y{portfolio.invested:,.0f}")
    if not suppress_push:
        broker.update({"status": "signals_generated", "progress": "5/5",
                    "n_positions": portfolio.positions, "invested": portfolio.invested, "trace_id": tid, "signals": target_positions})

    if _store_in is None:
        store.close()
    elapsed = time.time() - t0
    # Persist to daily_signals — 实盘/调度器需要, 回测 (suppress_push=True) 跳过
    targets = results.get("target_positions", [])
    if targets and not suppress_push:
        from quant.data.repos import TradeRepo
        TradeRepo(db_path=db_path).save_signals(date_str, targets, total_capital, strategy)
        logger.info(f"[pipeline] saved {len(targets)} targets to daily_signals for {date_str}")

    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"generate_signals done trace_id={tid} elapsed={elapsed:.1f}s phases=[{tracker.summary()}] date={date_str}")
    return results


def execute_signals(target_positions: list[dict], date_str: str, strategy: str = "quant",
                    prices: dict = None, db_path: str = TRADE_DB,
                    suppress_push: bool = False, ctx = None,
                    risk_only: bool = False) -> dict:
    """Pipeline 阶段二: 开盘执行 (Step 6)。

    prices: 预提供的开盘价dict (回测用); None则由fetch_quotes获取实时报价.
    db_path: 交易数据库路径 (回测用); None使用默认.
    suppress_push: True→不调用 broker.update (回测用).
    risk_only: True→只跑硬止损, 不再平衡 (weekly 非调仓日, rebalance_freq).
    """
    from quant.utils.logger import get_trace_id, set_trace_id as _set_tid
    tid = get_trace_id() or _uuid.uuid4().hex[:12]
    _set_tid(tid)
    from quant.monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")
    # A3: resolve dependencies from PipelineContext if provided
    if ctx is not None:
        db_path = db_path or ctx.db_path
        suppress_push = suppress_push or ctx.suppress_push
        # B-10 fix: execute_signals 没有 store/factor_store/preloaded_data/primitives/ic_map
        # 这些变量 — 原代码从 generate_signals 复制粘贴, 传 ctx 即 NameError.
        # execute 阶段只需要 db_path 与 suppress_push.

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    tracker = PhaseTracker("generate_signals")
    import time as _time_ph
    _ph_t0 = _time_ph.time()
    _ph_start = _time_ph.time()
    logger.info(f"execute_signals started trace_id={tid} date={date_str} strategy={strategy}")

    engine = ExecutionEngine(db_path=db_path)
    cost_model = CostModel.from_config()

    # Get current positions
    current_positions = engine.get_positions(strategy)
    logger.info(f"execute: {len(current_positions)} current positions, {len(target_positions)} target")

    # Build current lots map
    current_lots = {}
    for p in current_positions:
        current_lots[p["symbol"]] = p["shares"] // LOT_SIZE

    # Build target lots map
    target_lots = {}
    for tp in target_positions:
        sym = tp["symbol"]
        target_lots[sym] = tp["shares"] // LOT_SIZE

    # Load prices — 直接用 Sina 实时开盘价, 不走 market.db 回退.
    if prices is not None:
        # Backtest mode: use provided open prices directly
        prices = pd.Series(prices)
    else:
        # Live mode: fetch from Sina
        # 拉不到报价 → 不执行 (用错价格比不交易危害大, 且永不 fallback 制造隐形 bug).
        from quant.execution.quote import fetch_quotes
        symbols = list(set(list(current_lots.keys()) + list(target_lots.keys())))
        quotes = fetch_quotes(symbols)
        if not quotes:
            logger.error(
                f"execute: fetch_quotes returned empty for {len(symbols)} symbols — "
                f"skipping execution to avoid trading at stale prices"
            )
            return results

        prices = {}
        for sym, q in quotes.items():
            open_px = q.get("open", 0)
            if open_px > 0:
                prices[sym] = open_px
        # 报价未覆盖的持仓保留成本价 (仅用于估值, 不用于新买入)
        for p in current_positions:
            if p["symbol"] not in prices:
                prices[p["symbol"]] = p.get("price", 0)
        # 报价未覆盖的目标 (极罕见) 使用 sina price 而非昨日 close
        for tp in target_positions:
            if tp["symbol"] not in prices:
                q = quotes.get(tp["symbol"], {})
                prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
    prices = pd.Series(prices)

    # ── 统一执行链 (报告 §1.2/§6.1, ExecutionModel 重构) ──
    # 回测/实盘共用: 冷却过滤 → 固定止损 → delta → validate+按alpha裁剪 → 成交.
    # 行为变化: validate 失败原"全部丢弃"(回测过于悲观) → 现与实盘一致按
    # alpha 边际成本公式裁剪 (B-13). 止损标的当日剔除 + stopped_out 写入由模型完成.
    # pipeline 语义 = 按给定价格立即成交 → BacktestExecutionModel
    # (限价挂单语义在 scheduler/execute 的 LiveExecutionModel).
    from quant.execution.execution_model import (
        BacktestExecutionModel, ExecutionContext,
    )
    _exec_ctx = ExecutionContext(
        engine=engine, strategy=strategy, today=date_str, prices=prices,
        cost_model=cost_model,
    )
    _exec_res = BacktestExecutionModel().run(target_positions, _exec_ctx,
                                             risk_only=risk_only)
    orders = _exec_res.orders
    if _exec_res.stopped_out:
        # Q7-2 fix: stopped_out 必须写入 — loop.py 冷却依赖此字段 (原死代码)
        results["stopped_out"] = _exec_res.stopped_out

    results["steps"]["execution"] = {
        "orders": len(orders),
        "buys": sum(1 for o in orders if o.side == "buy"),
        "sells": sum(1 for o in orders if o.side == "sell"),
        "status": "ok",
    }
    logger.info(f"execute: {len(orders)} orders ({results['steps']['execution']['buys']} buys, {results['steps']['execution']['sells']} sells)")
    if not suppress_push:
        from web.state_broker import broker
        broker.update({"status": "trades_executed", "progress": "6/7", "orders": len(orders), "trace_id": tid, "signals": target_positions})
    _m.inc("pipeline.trades", len(orders))

    # ── Step 7: Monitor (实盘 only, 回测 suppress_push=True 跳过) ──
    if not suppress_push:
        positions = engine.get_positions(strategy)
        trades = engine.get_trades(strategy, limit=50)
        total_wealth = engine.get_capital(
            strategy, prices={s: float(v) for s, v in prices.items() if v and v > 0})
        cash_balance = engine.get_cash(strategy)
        from quant.data.repos import TradeRepo
        seed = TradeRepo(db_path=db_path).get_initial_capital(strategy)
        from quant.monitor.report import generate_report, push_to_web
        report = generate_report(
            date_str, cash_balance, positions, trades,
            pnl_total=total_wealth - seed,
            initial_capital=seed,
        )
        push_to_web(report)
        cap = report["capital"]
        results["steps"]["monitor"] = {
            "cash": cap["cash"], "positions_value": cap["positions_value"],
            "total_wealth": cap["total_wealth"],
            "total_return": report["metrics"]["total_return_pct"], "status": "ok",
        }
        logger.info(f"execute monitor: wealth=Y{cap['total_wealth']:,.2f} return={report['metrics']['total_return_pct']}%")

    elapsed = time.time() - t0
    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"execute_signals done trace_id={tid} elapsed={elapsed:.1f}s")
    return results





def run(date_str: str = None, capital: float = None, strategy: str = "quant", skip_pull: bool = False):
    """完整 Pipeline（向后兼容包装器）。

    阶段一: generate_signals() → 目标持仓
    阶段二: execute_signals() → 执行交易
    """
    signals = generate_signals(date_str, capital, strategy, skip_pull)
    if "target_positions" not in signals:
        logger.warning("generate_signals returned no target positions, skipping execution")
        return signals

    exec_result = execute_signals(signals["target_positions"], signals["date"], strategy)
    # Merge steps
    signals["steps"].update(exec_result.get("steps", {}))
    signals["elapsed_sec"] = signals.get("elapsed_sec", 0) + exec_result.get("elapsed_sec", 0)
    signals["stopped_out"] = exec_result.get("stopped_out", [])
# ── Trace recording (non-blocking) ──
    try:
        from quant.core.trace import get_trace, make_experiment, Hypothesis, ExperimentFeedback
        trace = get_trace()
        exp = make_experiment(
            action="pipeline_run",
            hypothesis=Hypothesis(
                hypothesis=f"Strategy {strategy} generates excess returns",
                reason=f"Pipeline run: factor eval + execution",
                source="pipeline.run()",
            ),
        )
        steps = signals.get("steps", {})
        exp.sub_results = {"date": signals.get("date"), "elapsed_sec": signals.get("elapsed_sec", 0)}
        exp.sub_results["steps_summary"] = {k: {sk: sv for sk, sv in v.items() if sk != "status"}
                                            for k, v in steps.items()}
        total_return = steps.get("monitor", {}).get("total_return", 0)
        exp.feedback = ExperimentFeedback(
            decision=total_return > 0,
            reason=f"Pipeline completed. Return: {total_return}",
            metrics={"total_return_pct": float(total_return) if total_return else 0.0},
        )
        trace.record(exp)
    except Exception as _e:
        logger.warning(f"Trace recording failed (non-blocking): {_e}")
    return signals




if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    capital_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    result = run(date_arg, capital_arg)
    import json
    print(json.dumps(result, indent=2, default=str))
