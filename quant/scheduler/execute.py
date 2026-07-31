"""交易执行调度器 — 每日 09:30.
ADR 033: 买入改为限价挂单, 由 monitor 盘中被动管理成交.
"""
import time as _time, uuid as _uuid
import pandas as pd
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from datetime import time, datetime
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.config.constants import _require_cfg
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.optimizer.portfolio import PortfolioConstructor
from quant.scheduler._base import _timed_loop

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("execute", today, grace_seconds=1800)  # 对齐 _TIMEOUTS (test-v301)
    if rid is None:
        _log.info(f"[{today}] execute already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 09:30 — executing trades")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:

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
            _log.error(f"[{today}] 今日无信号，拒绝执行 (no fallback)")
            error_msg = "no signals"
            _m.inc("scheduler.execute.no_targets")
            return
        if not _rebalance:
            targets = []  # risk_only 不使用 targets

        # ADR-036: 初始化 broker adapter (实盘路径 — 非 simulated 时尝试真实券商)
        from quant.execution.broker_adapter import get_broker_adapter, reset_adapter
        reset_adapter()  # 每天重置连接
        _broker = get_broker_adapter()
        _log.info(f"[{today}] broker adapter: {_broker.name} connected={_broker.is_connected()}")

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
            error_msg = "no quotes"
            _m.inc("scheduler.execute.no_quotes")
            return

        prices = {}
        for sym, q in quotes.items():
            open_px = q.get("open", 0)
            if open_px > 0:
                prices[sym] = open_px
        # 未覆盖持仓用成本价
        for p in current_positions:
            if p["symbol"] not in prices:
                prices[p["symbol"]] = p.get("price", 0)
        for tp in targets:
            if tp["symbol"] not in prices:
                q = quotes.get(tp["symbol"], {})
                prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
        prices = pd.Series(prices)

        # ── Step 3.5: 涨停封死预检 (test-v214) ──
        # 在挂单前检查 target 是否开盘即封死涨停，避免生成无效挂单
        # 封死的跳过，写入 exec_notes 供前端展示
        sealed_at_open = []
        for tp in targets:
            sym = tp["symbol"]
            q = quotes.get(sym, {})
            ask_vol = q.get("ask_volume", 0) or 0
            last_price = q.get("price", 0) or q.get("open", 0)
            prev_close = q.get("prev_close", 0)
            if prev_close <= 0 or last_price <= 0:
                continue
            # 判断涨停价 (板块差异化)
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
            # ── 重分配: 封板股释放的资本重新跑优化器, 剩余候选吃满资金 ──
            if targets:
                cash = engine.get_cash(strategy)
                alpha_series = pd.Series({tp["symbol"]: tp["score"] for tp in targets}, dtype=float)
                # B-18 fix: 重分配用实时开盘价 (注释声称实时价, 原代码实际用了
                # 信号里的昨收价 tp["price"]), 缺失标的回退信号价
                prices_series = pd.Series(
                    {tp["symbol"]: float(prices.get(tp["symbol"], tp["price"])) for tp in targets},
                    dtype=float)
                try:
                    opt = PortfolioConstructor()
                    # test-v307: 重分配走正常 construct 路径, price_buffer=0 (已有实时报价)
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
            # 重新构建 target_lots
            target_lots = {tp["symbol"]: tp["shares"] // LOT_SIZE for tp in targets}
            target_lots_series = pd.Series(target_lots, dtype=int) if target_lots else pd.Series(dtype=int)
            _log.info(f"[{today}] after sealed pre-check: {len(targets)} targets remain "
                      f"(removed {len(sealed_at_open)}: {sealed_at_open})")

        # ── Step 4-6: 统一执行链 (报告 §1.2/§6.1, ExecutionModel 重构) ──
        # 冷却过滤(DB持久化,Q7-2) → 固定止损+冷却登记 → delta
        # (skip_cash_feasibility=True, pipeline 已分配) → validate+按alpha裁剪(B-13)
        # → 卖市价成交 + 买限价挂单(ADR 033) + B-14 熔断检查.
        # 原 Step4 止损/Step5 delta裁剪/Step6 分单三段内联代码全部收敛到
        # LiveExecutionModel, 与回测 BacktestExecutionModel 共用同一执行链.
        from quant.execution.execution_model import (
            ExecutionContext, LiveExecutionModel,
        )
        _ctx = ExecutionContext(
            engine=engine, strategy=strategy, today=today, prices=prices,
            cost_model=cost_model, repo=repo,
        )
        _res = LiveExecutionModel().run(targets, _ctx, risk_only=not _rebalance)
        buys_done = _res.buys if _res.buys_mode == "limit_placed" else 0
        sells_done = _res.sells
        elapsed = _time.time() - t0
        _log.info(f"[{today}] execute done: {sells_done} sells, "
                  f"{buys_done} limit buys placed — elapsed={elapsed:.1f}s")
        _log.info(f"[SCHEDULER] {today} | TASK=execute | STATUS=OK | "
                 f"sells={sells_done} limit_buys={buys_done} | elapsed={elapsed:.1f}s")
        # B-03 fix: 只设置 status/summary, 由 finally 统一 _tk_finish 一次
        # (原来 try 内 finish("ok") + finally 再 finish("failed") → 每天抛 RuntimeError)
        status = "ok"
        summary = {"sells": sells_done, "limit_buys": buys_done, "elapsed": round(elapsed, 1)}
        _m.inc("scheduler.execute.ok")

    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] execute crashed: {e}")
        raise
    finally:
        _tk_finish("execute", today, status, error=error_msg, summary=summary)

def _loop():
    _timed_loop("execute", time(9, 30), _run, has_multiprocess=True)
