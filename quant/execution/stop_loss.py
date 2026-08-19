"""ATR 动态止损止盈 — 业界标准三重体系.

每条规则基于 ATR(20) 动态计算，不硬编码百分比:
  ATR = Wilder SMMA(max(H-L, |H-C_prev|, |L-C_prev|), 20)
        (v553: 原误用 SMA — docstring 称 EMA; Wilder 平滑 = 种子前20日SMA
         + 递归 (ATR*19+TR)/20, TradeStation/MultiCharts/vnpy 通用口径)

止盈三重:
  TP1 (2×ATR): 现价≥成本+2ATR → 卖半仓 (整手向下, <200股仅标记不卖)
  TP2 (3×ATR): 已TP1 且现价≥成本+3ATR → 卖剩余全部 (v553: 原只卖剩仓一半,
       偶数整手永远留 25% 尾巴, 横盘时无限期持有)
  移动锁利: 盈利超2ATR后, 从最高点回撤1.5ATR → 全卖

止损:
  初始止损: 现价≤成本-2ATR → 全卖
  移动止损: 盈利≥TP1水平后 现价≤最高-2ATR → 全卖
  移动止损: 盈利≥TP1水平后 现价≤最高-2ATR → 全卖 (触发线 = peak-2×ATR
       ≥ 成本+0.5×ATR, TP1 后保本由它数学覆盖 — v553 审查确认, 不另设 breakeven)
  时间止损: 持仓>20天+浮亏 → 全卖; 持仓>40天(2×max_hold_days)无条件 → 全卖 (v553)
  ATR兜底: 上市<21日 ATR不可用 → 固定 stop_loss_pct 止损 (v553: 原静默跳过,
       新股完全裸奔 — 违背零 fallback 哲学)

每个信号带 kind 字段 (profit/loss), 供 monitor 判定出场性质与冷却分档。

集成: quant/scheduler/monitor.py 盘中循环调用; execute.py 开盘 ATR 止损 (v553)
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


def _wilders_atr_from_trs(trs: list, period: int = 20) -> float:
    """Wilder SMMA ATR 纯函数 (v553): 种子 = 前 period 个 TR 的 SMA,
    之后 ATR_t = (ATR_{t-1}×(period-1) + TR_t) / period. 供测试直接验证."""
    if len(trs) < period:
        return 0.0
    atr = float(np.mean(trs[:period]))
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _compute_atr(symbol: str, period: int = 20, as_of: str = None) -> float:
    """从 market.db daily 表计算 ATR(period), PIT 截止到 as_of 前一天. 缓存 120 秒.

    v553: Wilder SMMA 平滑 (原 SMA); 需要全历史递归, 拉取全部 date < as_of 行.
    """
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
        "ORDER BY date ASC",
        (symbol, as_of)
    ).fetchall()
    conn.close()

    if len(rows) < period + 1:
        return 0.0  # v553: 上市 < period+1 日 → 0, 调用方 fallback 固定%止损

    prev_close = rows[0][2]  # 最早一天收盘
    trs = []
    for high, low, close in rows[1:]:
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    atr = _wilders_atr_from_trs(trs, period)
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

            gain = cur - cost
            pnl_pct = gain / cost

            # ── v553: ATR 不可用 (上市<period+1日) → 固定百分比止损兜底,
            # 原静默 continue = 新股裸奔, 无任何止损保护 ──
            if atr <= 0:
                _sl_pct = _require_cfg("risk.stop_loss_pct")
                if pnl_pct <= -_sl_pct:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "kind": "loss",
                                    "reason": "hard_sl_pct({:.1%})".format(pnl_pct)})
                continue

            # ── 已触发的目标标记 (从持仓额外字段或存储读) ──
            # B9 (2026-08-18): 跨日回载 _tp1_hit/_peak — 原 get_positions 不加载,
            # 导致 TP1 每日重复触发减半、移动止损跨日峰值丢失 (回测与实盘均受影响).
            # 存储模式与 cooloff 同构: dict=内存(回测, 实例跨日共享), None=DB(实盘,
            # MAX 聚合历史峰值, 跨重启存活). 调用方已显式注入字段时不再回载.
            # v532 (2026-08-18): 清仓重买判定 — 全历史 MAX 聚合把旧仓 peak/tp1
            # 带进新仓 (trailing 立即触发 / TP1 永久失效); 最近卖出时间晚于本仓
            # 买入时间 → 新仓, 跳过回载 (peak=成本, tp1=False)。
            if "_tp1_hit" not in p or "_peak" not in p:
                _meta = {}
                if not (p.get("buy_time") and self._is_recently_rebought(sym, p.get("buy_time"))):
                    _meta = self._meta_get(sym)
                if _meta:
                    p.setdefault("_tp1_hit", _meta.get("_tp1_hit", False))
                    p.setdefault("_peak", _meta.get("_peak") or cost)
                p["_loaded_meta"] = _meta  # v553: 供末尾变化检测 (仅变化时写库)
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
                                    "price": cur, "kind": "profit",
                                    "reason": "TP1(+{:.1f}ATR)".format(self.atr_mult_tp1)})
                tp1_hit = True
                p["_tp1_hit"] = True  # 持久化, 防止同轮次重复触发 (2026-07-21 audit C6)

            elif tp1_hit and gain >= self.atr_mult_tp2 * atr:
                # v553: TP2 清仓 — 原 rest = shares - 再一半, 偶数整手剩仓
                # (200/400/600股) 永远留一半, 最终 25% 仓位靠 trail_lock/横盘无限期持有
                results.append({"symbol": sym, "action": "sell", "shares": shares,
                                "price": cur, "kind": "profit",
                                "reason": "TP2(+{:.1f}ATR)".format(self.atr_mult_tp2)})

            elif tp1_hit and peak > cost + self.atr_mult_tp1 * atr:
                dd_from_peak = (peak - cur) / peak if peak > 0 else 0
                if dd_from_peak >= self.atr_mult_trail * atr / peak:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "kind": "profit",
                                    "reason": "trail_lock({:.1f}ATR dd)".format(self.atr_mult_trail)})
                    continue

            # ════════════════════════════════
            # 止损
            # ════════════════════════════════
            # v553 审查 #9 (TP1 后保本): 经数学验证为误报 — trail_lock/trail_sl
            # 触发线 = peak - sl×ATR ≥ (cost+tp1×ATR) - sl×ATR = cost + 0.5×ATR
            # (tp1_hit → peak ≥ cost+2×ATR, sl=2) 恒高于成本, TP1 后任何回落到
            # 保本线的价格必先触发移动止损 (峰值回撤), 利润不会吐光。
            # 故不引入 breakeven 死代码分支; hard_sl (成本-2ATR) 保留为极端闪崩底线。
            if gain <= -self.atr_mult_sl * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares,
                                "price": cur, "kind": "loss",
                                "reason": "hard_sl(-{:.1f}ATR)".format(self.atr_mult_sl)})
                continue

            # C4: trail_sl 需盈利 ≥ TP1 水平 (peak ≥ cost + tp1×ATR) 才启用 —
            # 原仅 peak>cost 即启用, 微利 (+0.25ATR) 波动即触发, 属噪音出场.
            if peak >= cost + self.atr_mult_tp1 * atr \
                    and (peak - cur) >= self.atr_mult_sl * atr:
                results.append({"symbol": sym, "action": "sell", "shares": shares,
                                "price": cur, "kind": "loss",
                                "reason": "trail_sl({:.1f}ATR from peak)".format(self.atr_mult_sl)})
                continue

            # time stop — v553: 盈利停滞退出 — 持仓 > 2×max_hold_days 无条件退出
            # (原仅浮亏触发, 盈利横盘仓无限期占用资金 = opportunity cost)
            buy_time = p.get("buy_time", "")
            if buy_time:
                from datetime import datetime as _dt
                days = (_dt.strptime(today, "%Y-%m-%d") - _dt.strptime(buy_time[:10], "%Y-%m-%d")).days
                if pnl_pct < 0 and days > self.max_hold_days:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "kind": "loss",
                                    "reason": "time_stop({}d)".format(days)})
                elif days > self.max_hold_days * 2:
                    results.append({"symbol": sym, "action": "sell", "shares": shares,
                                    "price": cur, "kind": "loss",
                                    "reason": "time_stop_hard({}d)".format(days)})

        # test-v313 + B9: 持久化峰值和止盈标记 (进程重启后恢复)
        # B9 (2026-08-18): 存储模式与 cooloff 同构 — dict=内存(回测), None=DB(实盘)
        # v553: 仅状态变化时写 (原每 30s 全持仓写库 = 写放大)
        self._today = today
        try:
            for p in positions:
                if not (p.get("_peak") or p.get("_tp1_hit")):
                    continue
                cur_peak = p.get("_peak") or 0
                cur_tp1 = bool(p.get("_tp1_hit"))
                loaded = p.get("_loaded_meta") or {}
                changed = (cur_tp1 and not loaded.get("_tp1_hit", False)) or \
                          (cur_peak > (p.get("price") or 0)
                           and cur_peak > (loaded.get("_peak") or 0))
                if changed:
                    self._meta_set(p["symbol"], cur_tp1, cur_peak)
        except Exception as _e:
            _log.debug("position_meta save failed (non-fatal): %s", _e)

        return results

    # ═══════════════════════════════════════════
    # B9: 止损状态存储 (tp1_hit / peak) — 与 cooloff 同构双模式
    # ═══════════════════════════════════════════

    def _is_recently_rebought(self, symbol: str, buy_time: str) -> bool:
        """v532: 本仓买入晚于最近一次卖出 → 清仓重买的新仓.

        仅 DB 模式有意义 (内存模式 _meta_store 由实例生命周期管理);
        实盘全历史 MAX 聚合必须靠此判定避免旧仓状态污染新仓。
        """
        if self._meta_store is not None:
            return False
        if not buy_time:
            return False
        from quant.data.repos.trade_repo import TradeRepo
        try:
            last_sell = TradeRepo().get_last_sell_time(symbol)
            return bool(last_sell) and buy_time[:19] > last_sell[:19]
        except Exception as _e:
            _log.debug("last_sell query failed for %s (non-fatal): %s", symbol, _e)
            return False

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
