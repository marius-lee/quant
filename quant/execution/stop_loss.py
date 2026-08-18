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
_CACHE_MAX = 4096  # test-v466 (BT-2): 有界 — 回测2年×多持仓会无限增长


def _compute_atr(symbol: str, period: int = 20, as_of: str = None) -> float:
    """从 market.db daily 表计算 ATR(period), PIT 截止到 as_of 前一天. 缓存 120 秒."""
    if as_of is None:
        raise ValueError("as_of is required — 回测必须传入当天日期, 防止未来行情前视")
    now = __import__('time').time()
    key = (symbol, period, as_of)
    if key in _CACHE:
        val, ts = _CACHE[key]
        if now - ts < 120:
            return val

    # P0-4 fix + test-v466 (BT-2): WHERE date < ? — 严格取执行日前一日及以前,
    # 原 date <= as_of 混入执行日日内行情 (当日 high/low 未收盘, 隐含前视)。
    conn = DatabaseManager.market()
    rows = conn.execute(
        "SELECT high, low, close FROM daily WHERE symbol=? AND date < ? "
        "ORDER BY date DESC LIMIT ?",
        (symbol, as_of, period + 1)
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
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
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
        # B9: 止损状态存储 (tp1_hit/peak) 与 cooloff 同模式 —
        # dict=内存(回测, RiskManager 实例跨日共享), None=DB(实盘, 跨重启存活)
        self._meta_store = {} if cooloff_store is not None else None
        self._today = ""

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

    def check(self, positions: list, quotes: dict, today: str,
              atr_panel: dict = None) -> list:
        """返回触发信号列表.

        atr_panel (test-v466, BT-2): {date_str: {symbol: atr}} 预计算面板 —
        回测热路径注入后免每仓每日 SQLite 查询; 缺失时回退实时计算。
        """
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

            _panel_atr = None
            if atr_panel and today in atr_panel:
                _panel_atr = atr_panel[today].get(sym)
            if _panel_atr is not None:
                atr = float(_panel_atr)
            else:
                atr = _compute_atr(sym, self.atr_period, today)
            if atr <= 0:
                continue

            gain = cur - cost
            pnl_pct = gain / cost
            atr_pct = atr / cost

            # ── 已触发的目标标记 (从持仓额外字段或存储读) ──
            # B9 (2026-08-18): 跨日回载 _tp1_hit/_peak — 原 get_positions 不加载,
            # 导致 TP1 每日重复触发减半、移动止损跨日峰值丢失 (回测与实盘均受影响).
            # 存储模式与 cooloff 同构: dict=内存(回测, 实例跨日共享), None=DB(实盘,
            # MAX 聚合历史峰值, 跨重启存活). 调用方已显式注入字段时不再回载.
            if "_tp1_hit" not in p or "_peak" not in p:
                _meta = self._meta_get(sym)
                if _meta:
                    p.setdefault("_tp1_hit", _meta.get("_tp1_hit", False))
                    p.setdefault("_peak", _meta.get("_peak") or cost)
            tp1_hit = p.get("_tp1_hit", False)
            peak = max(p.get("_peak", cost), cur)
            p["_peak"] = peak  # 持久化peak, trailing stop需要历史峰值 (2026-07-21 audit H8)

            # ════════════════════════════════
            # 止盈
            # ════════════════════════════════
            # C4 (CODE-REVIEW): TP1 卖一半但不少于一手, 一手残量则全卖 —
            # 原 max(100, shares//2//100*100): 100 股时 =100 (卖全仓), 300 股时=100 ✓,
            # 但 200 股时 =100 ✓ 且 100 股时语义错. 修: half 取整手向下, 至少 1 手,
            # 不超过持仓 (留一半)。
            # B8 (2026-08-18): 不足两手 (<200股) 无法对半卖整手 → 不卖, 仅标记
            # tp1_hit, 等 TP2/trail 全卖 — 原 100 股时 TP1 即全仓卖出, 提前清仓.
            if not tp1_hit and gain >= self.atr_mult_tp1 * atr:
                half_lots = shares // 2 // 100
                if half_lots >= 1:
                    sell_shares = half_lots * 100
                    if sell_shares >= shares:
                        sell_shares = shares
                    results.append({"symbol": sym, "action": "sell", "shares": sell_shares,
                                    "price": cur, "reason": "TP1(+{:.1f}ATR)".format(self.atr_mult_tp1)})
                tp1_hit = True
                p["_tp1_hit"] = True  # 持久化, 防止同轮次重复触发 (2026-07-21 audit C6)

            elif tp1_hit and gain >= self.atr_mult_tp2 * atr:
                # C4: TP2 卖剩仓 — 不可能再 =0 (修前 100股残留时算 0)
                rest = shares - max(100, (shares // 2 // 100) * 100)
                if rest <= 0 or rest > shares:
                    rest = shares
                results.append({"symbol": sym, "action": "sell", "shares": rest,
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

            # C4: trail_sl 需盈利 ≥ TP1 水平 (peak ≥ cost + tp1×ATR) 才启用 —
            # 原仅 peak>cost 即启用, 微利 (+0.25ATR) 波动即触发, 属噪音出场.
            if peak >= cost + self.atr_mult_tp1 * atr \
                    and (peak - cur) >= self.atr_mult_sl * atr:
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

        # test-v313 + B9: 持久化峰值和止盈标记 (进程重启后恢复)
        # B9 (2026-08-18): 存储模式与 cooloff 同构 — dict=内存(回测), None=DB(实盘)
        self._today = today
        try:
            for p in positions:
                if p.get("_peak") or p.get("_tp1_hit"):
                    self._meta_set(p["symbol"],
                                   p.get("_tp1_hit", False),
                                   p.get("_peak", 0))
        except Exception as _e:
            _log.debug("position_meta save failed (non-fatal): %s", _e)

        return results

    # ═══════════════════════════════════════════
    # B9: 止损状态存储 (tp1_hit / peak) — 与 cooloff 同构双模式
    # ═══════════════════════════════════════════

    def _meta_get(self, symbol: str) -> dict:
        """回载 symbol 的跨日止损状态. 内存模式: 进程内累计 (回测);
        DB 模式: MAX 聚合历史峰值 (实盘, 跨重启存活)."""
        if self._meta_store is not None:
            return self._meta_store.get(symbol, {})
        from quant.data.repos.trade_repo import TradeRepo
        try:
            return TradeRepo().get_position_meta_max(symbol)
        except Exception as _e:
            _log.debug("position_meta load failed for %s (non-fatal): %s", symbol, _e)
            return {}

    def _meta_set(self, symbol: str, tp1_hit: bool, peak: float):
        """写入止损状态. 内存模式: 峰值单调累计 (跨日); DB 模式: 当日行持久化."""
        if self._meta_store is not None:
            cur = self._meta_store.get(symbol, {})
            self._meta_store[symbol] = {
                "_tp1_hit": tp1_hit or cur.get("_tp1_hit", False),
                "_peak": max(peak, cur.get("_peak", 0) or 0),
            }
            return
        from quant.data.repos.trade_repo import TradeRepo
        TradeRepo().save_position_meta(symbol, self._today, tp1_hit, peak)
