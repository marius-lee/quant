"""交易执行调度器 — 每日 09:30.
ADR 033: 买入改为限价挂单, 由 monitor 盘中被动管理成交.

注意: task_log 由 Runner 统一管理，任务模块不再调用 _tk_start/_tk_finish。
"""
import time as _time, uuid as _uuid
import pandas as pd
from datetime import time, datetime
from quant.utils.date import today_str
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.config.constants import _require_cfg
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.optimizer.portfolio import PortfolioConstructor

_log = get_logger(__name__)

# v621 fix: 恢复 task_log 调用（InlineRunner 和 Dagster 都依赖此记录）
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish


def _should_rebalance(current_positions: list[dict], target_positions: list[dict],
                       quotes: dict, prices: dict, today: str) -> tuple[bool, str]:
    """v628: 智能调仓决策 — 仅当推荐股票显著优于当前持仓时才调仓.

    业务逻辑 (用户要求):
    1. 每天推荐的 alpha 股票池应该是增长潜力最高的股票
    2. 如果已买入的股票表现比推荐的好 → 继续持有
    3. 如果推荐的股票比已买入的好 → 调仓

    比较方法:
    - 对每个当前持仓，计算其 momentum (price / prev_close - 1)
    - 对每个推荐股票，获取其 alpha score
    - 如果推荐股票的 score 总和 > 当前持仓的 momentum 总和 + min_score_improvement
    → 调仓; 否则跳过 (risk_only)
    """
    if not current_positions and target_positions:
        return True, "no_current_positions"
    if not target_positions and current_positions:
        return True, "clear_all"
    if not target_positions and not current_positions:
        return False, "empty_both"

    _min_improvement = _require_cfg("optimizer.min_score_improvement",
                                     default=0.3)
    # 计算当前持仓的价格动量 (今日相对于前日收盘)
    current_momentum = 0.0
    for p in current_positions:
        sym = p["symbol"]
        q = quotes.get(sym, {})
        last_price = q.get("price", 0) or q.get("open", 0)
        prev_close = q.get("prev_close", 0)
        cost = p.get("avg_cost", p.get("cost", 0))
        if prev_close > 0 and last_price > 0:
            current_momentum += (last_price / prev_close - 1) * 100
        elif cost > 0 and last_price > 0:
            current_momentum += (last_price / cost - 1) * 100

    # 计算推荐股票的 alpha score 总和
    target_score = sum(t.get("score", 0) for t in target_positions)

    # 比较: 如果推荐分数显著高于当前动量 → 调仓
    improvement = target_score - current_momentum
    _log.info(f"[{today}] rebalance check: target_score={target_score:.2f}, "
              f"current_momentum={current_momentum:.2f}%, improvement={improvement:.2f}%")

    if improvement >= _min_improvement:
        _log.info(f"[{today}] improvement={improvement:.2f}% >= min={_min_improvement:.2f}%, "
                 f"rebalancing (new signals outperform)")
        return True, "score_improvement"
    else:
        _log.info(f"[{today}] improvement={improvement:.2f}% < min={_min_improvement:.2f}%, "
                 f"keeping current positions (outperform)")
        return False, "no_improvement"


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    _log.info(f"[{today}] 09:30 — executing trades")
    t0 = _time.time()

    # v621 fix: 写入 task_runs 记录
    _tk_start("execute", today)

    from quant.data.repos import TradeRepo
    repo = TradeRepo()
    LOT_SIZE = _require_cfg("backtest.lot_size")
    strategy = "quant"

    # ── rebalance_freq: 非调仓日只跑风控 (硬止损), 不要求有信号 ──
    from quant.execution.calendar import is_rebalance_day
    from quant.config.constants import _require_cfg as _ecfg
    _rebalance = is_rebalance_day(datetime.strptime(today, "%Y-%m-%d").date())
    _freq = _ecfg("optimizer.rebalance_freq")
    if not _rebalance:
        _log.info(f"[{today}] 非调仓日 (rebalance_freq={_freq}): risk-only 模式, "
                  f"不建仓/不调仓, 仅硬止损")

    # ── Step 1: 读取信号 + 持仓 ──
    sig = TradeRepo().get_latest_signals()
    targets = sig["targets"] if sig and sig["date"] == today else []
    signals_date = sig["date"] if sig else "未知"
    _log.info(f"[{today}] read {len(targets)} targets from daily_signals "
              f"(generated {signals_date})")

    if not targets and _rebalance:
        _log.warning(f"[{today}] 今日无信号, 跳过执行 (business idle, ok)")
        _m.inc("scheduler.execute.no_targets")
        _tk_finish("execute", today, "ok", summary={"sells": 0, "limit_buys": 0, "elapsed": round(_time.time() - t0, 1), "reason": "no_rebalance"})
        return {"reason": "no_rebalance", "targets": 0}
    if not _rebalance:
        targets = []  # risk_only 不使用 targets

    # ADR-036: 初始化 broker adapter (实盘路径 — 非 simulated 时尝试真实券商)
    # 修复: 确保事件循环存在 (asyncio.get_event_loop() 在非 async 线程会报错)
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    from quant.execution.broker_adapter import get_broker_adapter, reset_adapter
    reset_adapter()  # 每天重置连接
    _broker = get_broker_adapter()
    _log.info(f"[{today}] broker adapter: {_broker.config.name} connected={_broker.is_connected()}")

    engine = ExecutionEngine(broker_adapter=_broker)
    cost_model = CostModel.from_config()
    current_positions = engine.get_positions(strategy)
    _log.info(f"[{today}] {len(current_positions)} current positions")

    # ── Step 2: 构建 lots map ──
    current_lots = {}
    for p in current_positions:
        current_lots[p["symbol"]] = p["shares"] // LOT_SIZE

    target_lots = {}
    for tp in targets:
        sym = tp["symbol"]
        target_lots[sym] = tp["shares"] // LOT_SIZE

    # ── Step 3: 获取实时报价 ──
    from quant.execution.quote import fetch_quotes
    all_syms = list(set(list(current_lots.keys()) + list(target_lots.keys())))
    quotes = fetch_quotes(all_syms, include_ask_bid=True)
    if not quotes:
        _log.error(f"[{today}] 无实时报价, 拒绝执行 (no fallback)")
        _m.inc("scheduler.execute.no_quotes")
        raise RuntimeError("no quotes")

    prices = {}
    for sym, q in quotes.items():
        open_px = q.get("open", 0)
        if open_px > 0:
            prices[sym] = open_px
    # P1-15 fix: 缺报价持仓不使用成本价 (阻断卖单)
    _uncovered = [p["symbol"] for p in current_positions if p["symbol"] not in prices]
    if _uncovered:
        _log.warning(
            f"P1-15: {len(_uncovered)} positions without quotes, blocking sells: "
            f"{', '.join(_uncovered[:10])}"
        )
    for tp in targets:
        if tp["symbol"] not in prices:
            q = quotes.get(tp["symbol"], {})
            prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
    prices = pd.Series(prices)

    # ── v628: 智能调仓决策 (解决机械每日调仓问题) ──
    # 业务逻辑:
    #   1. 每天推荐的 alpha 股票池应该是增长潜力最高的股票
    #   2. 如果已买入的股票表现比推荐的好 → 继续持有
    #   3. 如果推荐的股票比已买入的好 → 调仓
    if _rebalance and targets and current_positions:
        _do_rebalance, _reason = _should_rebalance(
            current_positions, targets, quotes, prices, today)
        if not _do_rebalance:
            _log.info(f"[{today}] v628: smart rebalance → 跳过调仓 (reason={_reason}), "
                     f"进入 risk-only 模式")
            _rebalance = False
            targets = []  # risk_only 不使用 targets

    # ── Step 3.5: 涨停封死预检 (test-v214) ──
    sealed_at_open = []
    for tp in targets:
        sym = tp["symbol"]
        q = quotes.get(sym, {})
        ask_vol = q.get("ask_volume", 0) or 0
        last_price = q.get("price", 0) or q.get("open", 0)
        prev_close = q.get("prev_close", 0)
        if prev_close <= 0 or last_price <= 0:
            continue
        if sym.startswith("68") or sym.startswith("30"):
            limit_pct = 0.20
        elif sym[:1] == "4" or sym[:1] == "8" or sym.startswith("92"):
            limit_pct = 0.30
        else:
            limit_pct = 0.10
        limit_up_price = round(prev_close * (1 + limit_pct), 2)
        if abs(last_price - limit_up_price) <= 0.02 and ask_vol == 0:
            sealed_at_open.append(sym)
            repo.update_signal_exec_note(today, sym, "sealed_at_open")
            _log.info(f"[{today}] {sym} 开盘封死涨停 (ask=0, px={last_price}), skip")
    if sealed_at_open:
        targets = [tp for tp in targets if tp["symbol"] not in sealed_at_open]
        if targets:
            cash = engine.get_cash(strategy)
            alpha_series = pd.Series({tp["symbol"]: tp["score"] for tp in targets}, dtype=float)
            prices_series = pd.Series(
                {tp["symbol"]: float(prices.get(tp["symbol"], tp["price"])) for tp in targets},
                dtype=float)
            try:
                opt = PortfolioConstructor()
                new_pf = opt.construct(alpha_series, prices_series, cash, price_buffer=0.0)
                new_lots = new_pf.lots
                for tp in targets:
                    sym = tp["symbol"]
                    if sym in new_lots.index and new_lots[sym] > 0:
                        tp["shares"] = int(new_lots[sym]) * LOT_SIZE
                _log.info(f"[{today}] reallocated after sealed removal: " +
                          str({s: int(l) * LOT_SIZE for s, l in new_lots.items() if l > 0}))
            except Exception as _re_e:
                _log.warning(f"[{today}] reallocation after sealed failed (non-fatal): {_re_e}, "
                             f"using original allocations")
        target_lots = {tp["symbol"]: tp["shares"] // LOT_SIZE for tp in targets}
        target_lots_series = pd.Series(target_lots, dtype=int) if target_lots else pd.Series(dtype=int)
        _log.info(f"[{today}] after sealed pre-check: {len(targets)} targets remain "
                  f"(removed {len(sealed_at_open)}: {sealed_at_open})")

    # ── Step 3.6: 跌停卖出预检 (v412) ──
    limit_down_symbols = []
    for p in current_positions:
        sym = p["symbol"]
        q = quotes.get(sym, {})
        bid_vol = q.get("bid_volume", 0) or 0
        last_price = q.get("price", 0) or q.get("open", 0)
        prev_close = q.get("prev_close", 0)
        if prev_close <= 0 or last_price <= 0:
            continue
        limit_pct = 0.20 if sym.startswith(("68", "30")) else (0.30 if sym[:1] in ("4", "8") or sym.startswith("92") else 0.10)
        limit_down_price = round(prev_close * (1 - limit_pct), 2)
        if abs(last_price - limit_down_price) <= 0.02 and bid_vol == 0:
            limit_down_symbols.append(sym)
            _log.info(f"[{today}] {sym} 一字跌停 (bid=0, px={last_price}), skip sell")
    if limit_down_symbols:
        current_positions = [p for p in current_positions if p["symbol"] not in limit_down_symbols]

    # ── Step 3.7: 开盘 ATR 止损 (v553) ──
    # 双轨语义: ATR 止损 = 波动自适应出场; 固定 stop_loss_pct (check_hard_stop) =
    # 绝对最大亏损底线 — 并存取更早触发, 非重复冗余。
    # 原 09:30-09:35 空窗: execute 只跑固定 8%, ATR 止损 (monitor) 09:35 才启动 —
    # 跳空低开 -3%~-8% 区间无保护。此处用开盘价补跑完整 ATR 检查。
    from quant.execution.stop_loss import RiskManager as _RM
    _rm = _RM()
    _open_quotes = {p["symbol"]: {"price": float(prices.get(p["symbol"], 0) or 0)}
                    for p in current_positions}
    _open_sigs = _rm.check(current_positions, _open_quotes, today)
    # v560 (2026-08-19): 固定百分比硬止损同步进开盘 — 双轨止损 (固定% 底线 +
    # ATR 自适应) 与盘中 monitor 一致, 取更早触发。根因: 600331 8/14 击穿 -8%
    # 止损位但开盘检查仅跑 ATR, 固定硬止损盘中从不执行 (详见 monitor.py v560)。
    _hard_open = _rm.check_hard_stop(
        current_positions, {s: float(p) for s, p in prices.items() if p > 0})
    _open_sigs = _open_sigs + [h for h in _hard_open
                               if h["symbol"] not in {s["symbol"] for s in _open_sigs}]
    for _sig in _open_sigs:
        _sym = _sig["symbol"]
        _q = quotes.get(_sym, {})
        _bid = _q.get("bid_volume", 0) or 0
        _last = float(prices.get(_sym, 0) or 0)
        _prev = _q.get("prev_close", 0)
        if _prev > 0 and _last > 0:
            _lp = 0.20 if _sym.startswith(("68", "30")) else (
                0.30 if _sym[:1] in ("4", "8") or _sym.startswith("92") else 0.10)
            _ld = round(_prev * (1 - _lp), 2)
            if abs(_last - _ld) <= 0.02 and _bid == 0:
                _log.warning(f"[{today}] open-stop {_sym} 跌停封死 (bid=0), "
                             f"跳过 — 留给盘中 monitor 重试")
                continue
        _cost = next((pp.get("price", 0) for pp in current_positions
                      if pp["symbol"] == _sym), 0)
        _pnl = (_last / _cost - 1) * 100 if _cost > 0 else 0
        engine.execute([Order(symbol=_sym, side="sell", shares=_sig["shares"],
                              price=round(_sig["price"], 2), cost=5.0)],
                       today, strategy=strategy)
        _m.inc("scheduler.execute.open_stop")
        _log.warning(f"[{today}] OPEN-STOP: {_sym} {_sig['shares']}股 "
                     f"@¥{_sig['price']:.2f} ({_sig['reason']}) PnL={_pnl:+.1f}%")
        if _sig["reason"].startswith("hard_sl"):
            _rm.set_cooloff(_sym, today)
        elif _sig["reason"].startswith("trail_sl"):
            _rm.set_cooloff(_sym, today,
                            days=_require_cfg("risk.trail_sl_cooloff_days"))

    # ── Step 4-6: 统一执行链 ──
    from quant.execution.execution_model import (
        ExecutionContext, LiveExecutionModel,
    )
    _ctx = ExecutionContext(
        engine=engine, strategy=strategy, today=today, prices=prices,
        cost_model=cost_model, repo=repo,
        day_high={sym: float(q.get("high", 0) or 0) for sym, q in quotes.items()
                  if q.get("high")},
    )
    _res = LiveExecutionModel().run(targets, _ctx, risk_only=not _rebalance)
    buys_done = _res.buys if _res.buys_mode == "limit_placed" else 0
    sells_done = _res.sells
    elapsed = _time.time() - t0
    _log.info(f"[{today}] execute done: {sells_done} sells, "
              f"{buys_done} limit buys placed — elapsed={elapsed:.1f}s")
    _log.info(f"[SCHEDULER] {today} | TASK=execute | STATUS=OK | "
             f"sells={sells_done} limit_buys={buys_done} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.execute.ok")
    _tk_finish("execute", today, "ok", summary={"sells": sells_done, "limit_buys": buys_done, "elapsed": round(elapsed, 1), "reason": "no_rebalance" if not _rebalance else "ok"})
    return {"sells": sells_done, "limit_buys": buys_done, "elapsed": round(elapsed, 1)}


if __name__ == "__main__":
    import sys
    _run(sys.argv[1] if len(sys.argv) > 1 else today_str())