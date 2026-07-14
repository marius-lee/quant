"""盘中实时风控 — 每日 09:35-14:55 持续监控. P6 已落地.

Grinold & Kahn 标准: 盘中独立风控 daemon, 与执行引擎解耦。
检查项:
  1. 总资产回撤 / 资本熔断
  2. 单只盈亏止盈止损 (ATR)
  3. P6: 单票+单行业集中度监控
  4. P6: VaR 实时估算 (parametric)
  5. P6: 流动性过滤器 (日均成交额)
"""
import time as _time
import os
from datetime import datetime, time
from config.constants import _require_cfg
from utils.logger import get_logger
from monitor.metrics import metrics as _m

_log = get_logger("quant.scheduler.monitor")

# ── 风控阈值 (config-driven, 硬编码为默认值) ──
MAX_DRAWDOWN_PCT = 5.0
CIRCUIT_BREAKER_PCT = 5.0
CHECK_INTERVAL_SEC = 30
QUOTE_THROTTLE_SEC = 5  # 行情 API 限频


def _run_continuous(today: str):
    """盘中持续风控循环 — 09:35 到 14:55 每 30s 检查一次."""
    from quant.scheduler.status import register, update
    from web.state_broker import broker
    from execution.calendar import is_market_open

    register("monitor", "09:35-14:55", has_multiprocess=False)

    _log.info(f"[{today}] monitor started — interval={CHECK_INTERVAL_SEC}s")

    triggered_stop = set()   # 当日已触发: {"600036:profit", "000001:loss"}
    last_quote_ts = 0.0

    while True:
        now = datetime.now()
        hhmm = time(now.hour, now.minute)

        if hhmm >= time(14, 55):
            update("monitor", status="idle (收市)")
            _log.info(f"[{today}] monitor stopped — market closing")
            break

        if hhmm < time(9, 35) or not is_market_open():
            update("monitor", status="waiting (未开盘)")
            _time.sleep(_require_cfg("quant.scheduler.poll_interval"))
            continue

        # ── 盘中检查 ──
        state = broker.get()
        total = state.get("total_asset", 0) or 0
        initial = (state.get("metrics") or {}).get("initial_capital", 5000)
        positions = state.get("positions") or []

        alerts = []
        # ── P6-a: 加载集中度和 VaR 阈值 ──
        single_conc_limit = _require_cfg("monitor.max_single_concentration")
        sector_conc_limit = _require_cfg("monitor.max_sector_concentration")
        liquidity_min = _require_cfg("monitor.min_daily_turnover_amount")
        var_conf = _require_cfg("monitor.var_confidence")

        # 1. 总资产回撤 / 熔断
        if initial > 0 and total > 0:
            dd_pct = round((1 - total / initial) * 100, 1)
            if dd_pct > MAX_DRAWDOWN_PCT:
                alerts.append(f"回撤 {dd_pct}% > {MAX_DRAWDOWN_PCT}%")
            if total < initial * (1 - CIRCUIT_BREAKER_PCT / 100):
                alerts.append(f"熔断! ¥{total:,.0f} < ¥{initial*0.95:,.0f}")
                broker.update({"circuit_breaker": True,
                               "cb_reason": f"总资产 {total:,.0f} < 95%初始资金"})

        # 2. 止盈止损扫描 — 每 QUOTE_THROTTLE_SEC 拉一次行情
        if positions:
            now_ts = _time.time()
            if now_ts - last_quote_ts >= QUOTE_THROTTLE_SEC:
                last_quote_ts = now_ts
                syms = [p["symbol"] for p in positions]
                quotes = {}
                from execution.quote import fetch_quotes
                quotes = fetch_quotes(syms) or {}
                # ── P6-b: 单票+单行业集中度 ──
                if positions and total > 0:
                    for p in positions:
                        pos_val = p.get("shares", 0) * (quotes.get(p["symbol"], {}).get("price", 0) or p.get("price", 0))
                        if pos_val / total > single_conc_limit:
                            alerts.append(f"集中度告警: {p['symbol']} {pos_val/total*100:.0f}% > {single_conc_limit*100:.0f}%")
                    # 行业集中度
                    sector_vals = {}
                    for p in positions:
                        sec = p.get("sector", "未知")
                        pos_val = p.get("shares", 0) * (quotes.get(p["symbol"], {}).get("price", 0) or p.get("price", 0))
                        sector_vals[sec] = sector_vals.get(sec, 0) + pos_val
                    for sec, sv in sector_vals.items():
                        if sv / total > sector_conc_limit:
                            alerts.append(f"行业集中度告警: {sec} {sv/total*100:.0f}% > {sector_conc_limit*100:.0f}%")

                # ── P6-c: VaR 实时估算 ──
                if positions and total > 0:
                    try:
                        import pandas as pd
                        import numpy as np
                        syms_for_var = [p["symbol"] for p in positions[:50]]  # max 50 stocks
                        if len(syms_for_var) > 1:
                            conn2 = _get_market_conn()
                            close_rows = conn2.execute(
                                "SELECT symbol, date, close FROM daily WHERE symbol IN ({}) "
                                "AND date >= date('now', '-60 days') ORDER BY date".format(
                                    ",".join("?" * len(syms_for_var))),
                                syms_for_var
                            ).fetchall()
                            if close_rows:
                                df_close = pd.DataFrame(close_rows, columns=["symbol", "date", "close"])
                                piv = df_close.pivot(index="date", columns="symbol", values="close")
                                rets = piv.pct_change().dropna(how="all")
                                w = pd.Series({s: (sum(1 for p in positions if p["symbol"] == s) or 0) for s in syms_for_var})
                                if w.sum() > 0:
                                    w = w / w.sum()
                                    common_syms = [s for s in w.index if s in rets.columns]
                                    if len(common_syms) > 1:
                                        w_sub = w[common_syms]
                                        cov = rets[common_syms].cov()
                                        from risk.var import compute_var
                                        var_val = compute_var(total, w_sub, cov, confidence=var_conf)
                                        if var_val is not None and var_val > 0:
                                            var_pct = var_val / total * 100
                                            if var_pct > 3.0:
                                                alerts.append(f"VaR告警: daily VaR {var_pct:.1f}% ({var_val:,.0f})")
                    except Exception as e:
                        _log.debug(f"VaR check skipped (non-fatal): {type(e).__name__}")

                # ── P6-d: 流动性过滤器 ──
                if positions:
                    try:
                        conn2 = _get_market_conn()
                        for p in positions:
                            row = conn2.execute(
                                "SELECT AVG(amount) FROM daily WHERE symbol=? AND date >= date('now', '-20 days')",
                                (p["symbol"],)
                            ).fetchone()
                            if row and row[0] and row[0] < liquidity_min:
                                alerts.append(f"流动性告警: {p['symbol']} 日均成交 {row[0]:,.0f} < {liquidity_min/1e7:.0f}千万")
                    except Exception as e:
                        _log.debug(f"Liquidity check skipped (non-fatal): {type(e).__name__}")

                # ── P6-e: 交易频率监控 (R2: 防止过度交易) ──
                try:
                    max_trades = _require_cfg("monitor.max_trades_per_day")
                    max_daily_turnover = _require_cfg("monitor.max_daily_turnover_pct")
                    from execution.engine import ExecutionEngine
                    eng = ExecutionEngine()
                    today_trades = eng.get_trades(strategy="quant", limit=200)
                    today_cnt = sum(1 for t in today_trades if t.get("date") == today)
                    if today_cnt > max_trades:
                        alerts.append(f"交易频率告警: 今日{today_cnt}笔 > {max_trades}笔上限")

                    # 换手率 = 今日成交额 / 组合总资产
                    today_turnover = sum(
                        abs(t.get("price", 0) * t.get("shares", 0)) for t in today_trades
                        if t.get("date") == today
                    )
                    if today_turnover > 0 and total > 0:
                        turnover_pct = today_turnover / total
                        if turnover_pct > max_daily_turnover:
                            alerts.append(f"换手率告警: 今日{turnover_pct*100:.0f}% > {max_daily_turnover*100:.0f}%")
                except Exception as e:
                    _log.debug(f"Trade frequency check skipped (non-fatal): {type(e).__name__}")

                from execution.stop_loss import RiskManager as _RM
                rm = _RM()
                signals = rm.check(positions, quotes, today)
                for sig in signals:
                    sym = sig["symbol"]
                    cur = sig["price"]
                    sell_shares = sig["shares"]
                    reason = sig["reason"]
                    pnl_pct = 0.0
                    for p2 in positions:
                        if p2["symbol"] == sym:
                            cost = p2.get("price", 0)
                            if cost > 0:
                                pnl_pct = (cur / cost - 1)
                            break

                    if "TP" in reason.upper():
                        tp_key = f"{sym}:profit"
                        if tp_key not in triggered_stop:
                            _execute_sell(today, sym, sell_shares, cur, "止盈", round(pnl_pct * 100, 1))
                            triggered_stop.add(tp_key)
                            alerts.append(f"{sym} 止盈 {pnl_pct*100:.0f}% ({reason})")
                            _m.inc("scheduler.monitor.stop_profit")
                    else:
                        sl_key = f"{sym}:loss"
                        if sl_key not in triggered_stop:
                            _execute_sell(today, sym, sell_shares, cur, "止损", round(pnl_pct * 100, 1))
                            triggered_stop.add(sl_key)
                            alerts.append(f"{sym} 止损 {pnl_pct*100:.0f}% ({reason})")
                            _m.inc("scheduler.monitor.stop_loss")

        status = "ok" if not alerts else "⚠ " + "; ".join(alerts)
        if alerts:
            _log.warning(f"[{today}] MONITOR: {status}")
            _m.inc("scheduler.monitor.alert")
        else:
            _m.inc("scheduler.monitor.ok")

        update("monitor", status=status, last_run=now.isoformat())

        _time.sleep(CHECK_INTERVAL_SEC)


def _execute_sell(today: str, symbol: str, shares: int, price: float,
                  reason: str, pnl_pct: float):
    """执行卖出订单 + 写入 trades DB."""
    from execution.engine import ExecutionEngine, Order
    engine = ExecutionEngine()
    engine.execute(
        [Order(symbol=symbol, side="sell", shares=shares,
               price=round(price, 2), cost=5.0)],
        today, strategy="quant"
    )
    _log.warning(f"[{today}] {reason}: {symbol} {shares}股 @¥{price:.2f} "
                 f"PnL={pnl_pct:+.1f}%")


def _outer_loop():
    """外层循环: 每天等待到 09:35 后启动 _run_continuous."""
    from execution.calendar import is_trading_day

    today = None
    started = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")

        if current_day != today:
            today = current_day
            started = False

        if not started and is_trading_day():
            hhmm = time(now.hour, now.minute)
            if hhmm >= time(9, 35):
                started = True
                _run_continuous(today)

        _time.sleep(_require_cfg("quant.scheduler.poll_interval"))


def _loop():
    """启动风控监控 daemon 线程."""
    import threading
    t = threading.Thread(target=_outer_loop, daemon=True, name="sch-monitor")
    t.start()
    _log.info("monitor scheduler launched (09:35-14:55)")


def _get_market_conn():
    """获取 market.db 只读连接 (P6 辅助)."""
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "..", "data", "market.db"))
    return conn
