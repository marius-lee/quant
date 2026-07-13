"""ATR 动态止损止盈 — 业界标准三重体系.

每条规则基于 ATR(20) 动态计算，不硬编码百分比:
  ATR = EMA(max(H-L, |H-C_prev|, |L-C_prev|), 20)

止盈三重:
  TP1 (2×ATR): 现价≥成本+2ATR → 卖50%
  TP2 (3×ATR): 现价≥成本+3ATR → 卖剩余50%
  移动锁利: 盈利超2ATR后，从最高点回撤1.5ATR → 全卖

止损三重:
  初始止损: 现价≤成本-2ATR → 全卖
  移动止损: 现价≤最高-2ATR → 全卖
  时间止损: 持仓>20天+浮亏 → 全卖

集成: quant/scheduler/monitor.py 盘中循环调用
"""
import sqlite3, os
import numpy as np
from config.loader import get as _cfg
from utils.logger import get_logger

_log = get_logger("execution.stop_loss")

_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
_CACHE = {}  # symbol -> (atr, ts)


def _compute_atr(symbol: str, period: int = 20) -> float:
    """从 market.db daily 表实时计算 ATR(20). 缓存120秒."""
    now = __import__('time').time()
    key = (symbol, period)
    if key in _CACHE:
        val, ts = _CACHE[key]
        if now - ts < 120:
            return val

    try:
        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT high, low, close FROM daily WHERE symbol=? "
            "ORDER BY date DESC LIMIT ?",
            (symbol, period + 1)
        ).fetchall()
        conn.close()

        if len(rows) < period:
            return 0.0

        rows.reverse()  # 从旧到新
        tr_values = []
        prev_close = rows[0][2]  # 前一天收盘
        for high, low, close in rows[1:]:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
            prev_close = close

        atr = float(np.mean(tr_values)) if tr_values else 0.0
        _CACHE[key] = (atr, now)
        return atr
    except Exception as e:
        _log.error(f"_compute_atr({symbol}): {e}")
        return 0.0


class RiskManager:
    """无状态 — 每次 check() 传入持仓快照."""

    def __init__(self):
        self.atr_mult_sl = _cfg("risk.atr_mult_stop_loss")
        self.atr_mult_tp1 = _cfg("risk.atr_mult_take_profit_1")
        self.atr_mult_tp2 = _cfg("risk.atr_mult_take_profit_2")
        self.atr_mult_trail = _cfg("risk.atr_mult_trailing")
        self.max_hold_days = _cfg("risk.max_hold_days")
        self.atr_period = _cfg("risk.atr_period")

    def check(self, positions: list, quotes: dict, today: str) -> list:
        """返回触发信号列表."""
        results = []
        for p in positions:
            sym = p["symbol"]
            cost = p.get("price", 0)
            shares = p.get("shares", 0)
            if cost <= 0 or shares <= 0:
                continue

            q = quotes.get(sym, {})
            cur = q.get("price", 0) if q else 0
            if cur <= 0:
                cur = p.get("current") or cost
            if cur <= 0:
                continue

            atr = _compute_atr(sym, self.atr_period)
            if atr <= 0:
                continue

            gain = cur - cost
            pnl_pct = gain / cost
            atr_pct = atr / cost

            # ── 已触发的目标标记 (从持仓额外字段或缓存读) ──
            tp1_hit = p.get("_tp1_hit", False)
            peak = max(p.get("_peak", cost), cur)

            # ════════════════════════════════
            # 止盈
            # ════════════════════════════════
            if not tp1_hit and gain >= self.atr_mult_tp1 * atr:
                sell_shares = max(1, shares // 2)
                results.append({"symbol": sym, "action": "sell", "shares": sell_shares,
                                "price": cur, "reason": "TP1(+{:.1f}ATR)".format(self.atr_mult_tp1)})
                tp1_hit = True

            elif tp1_hit and gain >= self.atr_mult_tp2 * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares - shares // 2,
                                "price": cur, "reason": "TP2(+{:.1f}ATR)".format(self.atr_mult_tp2)})

            elif tp1_hit and peak > cost + self.atr_mult_tp1 * atr:
                dd_from_peak = (peak - cur) / peak if peak > 0 else 0
                if dd_from_peak >= self.atr_mult_trail * atr / peak:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "reason": "trail_lock({:.1f}ATR dd)".format(self.atr_mult_trail)})
                    continue

            # ════════════════════════════════
            # 止损
            # ════════════════════════════════
            if gain <= -self.atr_mult_sl * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares,
                                "price": cur, "reason": "hard_sl(-{:.1f}ATR)".format(self.atr_mult_sl)})
                continue

            if peak > cost and (peak - cur) >= self.atr_mult_sl * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares,
                                "price": cur, "reason": "trail_sl({:.1f}ATR from peak)".format(self.atr_mult_sl)})
                continue

            # time stop
            buy_time = p.get("buy_time", "")
            if buy_time and pnl_pct < 0:
                try:
                    from datetime import datetime as _dt
                    days = (_dt.strptime(today, "%Y-%m-%d") - _dt.strptime(buy_time[:10], "%Y-%m-%d")).days
                    if days > self.max_hold_days:
                        results.append({"symbol": sym, "action": "sell", "shares": shares,
                                        "price": cur, "reason": "time_stop({}d)".format(days)})
                except Exception:
                    import traceback as _tb_ts
                    _log.error("time_stop day parsing failed for %s (buy_time=%s): %s", sym, buy_time, _tb_ts.format_exc())

        return results
