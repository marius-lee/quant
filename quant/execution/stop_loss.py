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
import sqlite3
from quant.data.repos._base import DatabaseManager, os
import numpy as np
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("execution.stop_loss")

from quant.config.paths import MARKET_DB as _DB
_CACHE = {}  # symbol -> (atr, ts)


def _compute_atr(symbol: str, period: int = 20) -> float:
    """从 market.db daily 表实时计算 ATR(20). 缓存120秒."""
    now = __import__('time').time()
    key = (symbol, period)
    if key in _CACHE:
        val, ts = _CACHE[key]
        if now - ts < 120:
            return val

    # B-01 fix: 行情日线在 market.db, 不在 trades.db (trades.db 的 daily 表为空,
    # 导致 ATR 恒为 0, 盘中止盈止损全部静默失效)
    conn = DatabaseManager.market()
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


class RiskManager:
    """统一风控服务 (Q7-2 重构): 固定硬止损 + ATR 止损止盈 + 冷却注册表。

    调用点: pipeline(回测) / scheduler.execute(实盘开盘) / scheduler.monitor(盘中)。
    此前固定止损逻辑在三处各自复制, 冷却期只有回测实现且因 stopped_out
    从未写入而是死代码。

    cooloff_store: None → TradeRepo meta KV (实盘, 跨进程/重启存活);
                   dict → 内存 (回测热路径, 避免每日 DB 写)。
    """

    def __init__(self, strategy: str = "quant", cooloff_store=None):
        self.atr_mult_sl = _require_cfg("risk.atr_mult_stop_loss")
        self.atr_mult_tp1 = _require_cfg("risk.atr_mult_take_profit_1")
        self.atr_mult_tp2 = _require_cfg("risk.atr_mult_take_profit_2")
        self.atr_mult_trail = _require_cfg("risk.atr_mult_trailing")
        self.max_hold_days = _require_cfg("risk.max_hold_days")
        self.atr_period = _require_cfg("risk.atr_period")
        self.strategy = strategy
        self._cooloff_store = cooloff_store

    # ═══════════════════════════════════════════
    # 固定百分比硬止损 (回测/实盘开盘共用)
    # ═══════════════════════════════════════════

    def check_hard_stop(self, positions: list, prices: dict,
                        sl_pct: float = None) -> list:
        """固定百分比硬止损 — 现价跌破成本 sl_pct 触发全卖。

        返回: [{symbol, shares, price, cost, drop, reason}]
        此前该逻辑复制于 pipeline.execute_signals 与 scheduler/execute 两处。
        """
        if sl_pct is None:
            sl_pct = _require_cfg("risk.stop_loss_pct")
        stops = []
        for p in positions:
            cost = p.get("price", 0)
            cur = prices.get(p["symbol"], None)
            if cur is None or cost <= 0:
                continue
            try:
                if np.isnan(cur) or cur <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            drop = (float(cur) - cost) / cost
            if drop <= -sl_pct:
                shares = int(p.get("shares", 0))
                if shares > 0:
                    stops.append({"symbol": p["symbol"], "shares": shares,
                                  "price": float(cur), "cost": cost,
                                  "drop": drop,
                                  "reason": "hard_sl({:.1%})".format(drop)})
        return stops

    # ═══════════════════════════════════════════
    # 冷却注册表 (止损后 N 天禁止买回, 回测/实盘共用)
    # ═══════════════════════════════════════════

    def _cooloff_key(self) -> str:
        return f"cooloff:{self.strategy}"

    def _load_cooloff_db(self) -> dict:
        from quant.data.repos import TradeRepo
        import json as _json
        raw = TradeRepo().get_flag(self._cooloff_key())
        if not raw:
            return {}
        try:
            return _json.loads(raw)
        except Exception:
            return {}

    def _save_cooloff_db(self, data: dict):
        from quant.data.repos import TradeRepo
        import json as _json
        TradeRepo().set_flag(self._cooloff_key(), _json.dumps(data))

    def set_cooloff(self, symbol: str, today: str, days: int = None):
        """标记 symbol 进入冷却期 (默认 risk.stop_loss_cooloff_days 天)。"""
        if days is None:
            days = _require_cfg("risk.stop_loss_cooloff_days")
        from datetime import datetime as _dt, timedelta as _td
        end = (_dt.strptime(today, "%Y-%m-%d") + _td(days=days)).strftime("%Y-%m-%d")
        if self._cooloff_store is not None:
            self._cooloff_store[symbol] = end
        else:
            data = self._load_cooloff_db()
            data[symbol] = end
            self._save_cooloff_db(data)
        _log.info(f"cooloff set: {symbol} until {end} (strategy={self.strategy})")

    def get_cooloff_symbols(self, today: str) -> set:
        """今天仍在冷却期的 symbol 集合 (end > today, ISO 日期字符串可比)。"""
        if self._cooloff_store is not None:
            data = self._cooloff_store
        else:
            data = self._load_cooloff_db()
        return {s for s, end in data.items() if end > today}

    def clear_cooloff(self, symbol: str):
        """手动解除冷却 (如人工判断可买回)。"""
        if self._cooloff_store is not None:
            self._cooloff_store.pop(symbol, None)
        else:
            data = self._load_cooloff_db()
            if symbol in data:
                del data[symbol]
                self._save_cooloff_db(data)

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
            p["_peak"] = peak  # 持久化peak, trailing stop需要历史峰值 (2026-07-21 audit H8)

            # ════════════════════════════════
            # 止盈
            # ════════════════════════════════
            if not tp1_hit and gain >= self.atr_mult_tp1 * atr:
                sell_shares = max(100, (shares // 2 // 100) * 100)
                results.append({"symbol": sym, "action": "sell", "shares": sell_shares,
                                "price": cur, "reason": "TP1(+{:.1f}ATR)".format(self.atr_mult_tp1)})
                tp1_hit = True
                p["_tp1_hit"] = True  # 持久化, 防止同轮次重复触发 (2026-07-21 audit C6)

            elif tp1_hit and gain >= self.atr_mult_tp2 * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares - max(100, (shares // 2 // 100) * 100),
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
                from datetime import datetime as _dt
                days = (_dt.strptime(today, "%Y-%m-%d") - _dt.strptime(buy_time[:10], "%Y-%m-%d")).days
                if days > self.max_hold_days:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "reason": "time_stop({}d)".format(days)})

        # test-v313: 持久化峰值和止盈标记 (进程重启后恢复)
        try:
            from quant.data.repos.trade_repo import TradeRepo
            repo = TradeRepo()
            for p in positions:
                if p.get("_peak") or p.get("_tp1_hit"):
                    repo.save_position_meta(p["symbol"], today,
                        tp1_hit=p.get("_tp1_hit", False),
                        peak_price=p.get("_peak", 0))
        except Exception as _e:
            _log.debug("position_meta save failed (non-fatal): %s", _e)

        return results
