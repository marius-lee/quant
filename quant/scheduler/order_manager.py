"""限价单管理器 — 事件驱动的挂单/追价/补单。

ADR 033: 执行从"09:30 市价一次买入"改为"限价挂单 + 被动成交 + 尾盘补单"。
Monitor 每 5s 调一次 check_and_manage, 不靠定时轮询。

状态机: pending → filled | cancelled → (force_filled)
事件触发:
  A: ask ≤ limit_price → 成交
  C: ask > limit_price 且价差 > 紧迫度阈值(随时间衰减) → 放弃, 市价买入
  D: 时间 ≥ force_fill_time → 全部未成交市价补单
  V: 单量 > 日均成交量1% → 跳过 (流动性不足)

时间紧迫度曲线 (test-v252, 对齐 Kissell IS 算法):
  09:30: 容忍 3% 偏离 (不急, 开盘流动性好)
  11:30: 容忍 ~2% 偏离
  14:00: 容忍 ~1% 偏离
  14:30: 容忍 ~0.7% 偏离 (快收盘了, 赶紧买入)
  14:50: 不等待, 市价补单

(b-11 fix: 原事件 B "ask 远低于 limit → 追价下调" 不可达 — ask ≤ limit 已被
  事件 A 成交, 且买价走低时主动下调限价等于放弃更优成交价, 逻辑上也不成立, 已移除)
"""
from quant.utils.logger import get_logger
_log = get_logger(__name__)

import sqlite3
from datetime import datetime, time
from dataclasses import dataclass
from typing import Optional
from quant.config.constants import _require_cfg
from quant.execution.cost import CostModel
from quant.config.paths import TRADE_DB


DB_PATH = TRADE_DB

# ── 阈值 (config-driven) ──
DISCOUNT_PCT = _require_cfg("execution.limit_order.discount_pct")
RUNAWAY_START = _require_cfg("execution.limit_order.runaway_start")
RUNAWAY_END = _require_cfg("execution.limit_order.runaway_end")
VOLUME_LIMIT_PCT = _require_cfg("execution.limit_order.volume_limit_pct")
_force_fill_str = _require_cfg("execution.limit_order.force_fill_time")
_hh, _mm = _force_fill_str.split(":")
FORCE_FILL_TIME = time(int(_hh), int(_mm))
QUOTE_TTL_SEC = _require_cfg("execution.limit_order.quote_ttl_sec")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def ensure_table():
    """在 _ensure_tables 调用链之外独立创建 pending_orders 表。"""
    c = _conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL DEFAULT 'quant',
            symbol TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'buy',
            target_shares INTEGER NOT NULL,
            limit_price REAL NOT NULL,
            reference_price REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            placed_at TEXT NOT NULL,
            filled_at TEXT,
            filled_shares INTEGER DEFAULT 0,
            filled_price REAL,
            chase_count INTEGER DEFAULT 0,
            day TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_po_status ON pending_orders(status, day);
    """)
    c.commit()
    c.close()


@dataclass
class PendingOrder:
    id: int
    strategy: str
    symbol: str
    target_shares: int
    limit_price: float
    reference_price: float
    status: str
    placed_at: str
    chase_count: int
    day: str


class OrderManager:
    """限价单生命周期管理."""

    def __init__(self):
        ensure_table()

    # ── 挂单 ──
    def place(self, day: str, strategy: str,
              symbol: str, shares: int, ref_price: float) -> int:
        """挂限价买单: limit_price = ref_price × (1 - DISCOUNT_PCT)."""
        limit = round(ref_price * (1 - DISCOUNT_PCT), 2)
        c = _conn()
        now = datetime.now().isoformat(timespec="seconds")
        rid = c.execute(
            """INSERT INTO pending_orders
               (strategy, symbol, side, target_shares, limit_price,
                reference_price, status, placed_at, day)
               VALUES (?, ?, 'buy', ?, ?, ?, 'pending', ?, ?)""",
            (strategy, symbol, shares, limit, ref_price, now, day)
        ).lastrowid
        c.commit()
        _log.info(f"[order_manager] placed limit buy: {symbol} {shares}股 "
                  f"limit=¥{limit:.2f} (ref=¥{ref_price:.2f})")
        return rid

    # ── 获取当日未成交单 ──
    def get_pending(self, day: str, strategy: str = "quant") -> list[PendingOrder]:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM pending_orders WHERE day=? AND strategy=? AND status='pending'",
            (day, strategy)).fetchall()
        c.close()
        return [PendingOrder(
            id=r["id"], strategy=r["strategy"], symbol=r["symbol"],
            target_shares=r["target_shares"], limit_price=r["limit_price"],
            reference_price=r["reference_price"] or 0, status=r["status"],
            placed_at=r["placed_at"], chase_count=r["chase_count"] or 0,
            day=r["day"]) for r in rows]

    # ── 事件驱动管理 — 每个 5s 周期调用一次 ──
    def check_and_manage(self, day: str, quotes: dict,
                         strategy: str = "quant") -> list[dict]:
        """对每个 pending 订单评估事件:

        A: ask <= limit → execute fill
        C: ask > limit && gap > urgency(t) → abandon → market fill
        D: now >= force_fill_time → force fill all
        V: order_size > avg_daily_volume × 1% → skip (流动性不足)

        紧迫度: 时间衰减曲线, 对齐 Kissell IS 算法.
        """
        now = datetime.now()
        hhmm = time(now.hour, now.minute)
        force_now = hhmm >= FORCE_FILL_TIME

        pending = self.get_pending(day, strategy)
        actions = []

        for po in pending:
            q = quotes.get(po.symbol, {})
            ask = q.get("ask", None) or q.get("price", 0) or q.get("open", 0) or 0
            if ask <= 0:
                continue

            # ── V: 流动性检查 — 单量 > 日均量1% → 跳过 ──
            _daily_vol = q.get("volume", 0) or 0
            if _daily_vol > 0:
                _order_pct = po.target_shares / (_daily_vol * 100) if _daily_vol * 100 > 0 else 1.0
                if _order_pct > VOLUME_LIMIT_PCT and not force_now:
                    _log.info(f"[order_manager] SKIP {po.symbol}: "
                              f"order={po.target_shares}股 > {VOLUME_LIMIT_PCT*100:.0f}% vol, wait")
                    continue

            # ── 封死涨停检测 ──
            _av = q.get("ask_volume")
            if _av is not None and _av == 0 and not force_now:
                _pc = q.get("prev_close", 0) or 0
                if _pc > 0 and ask > 0:
                    _pct = (ask / _pc - 1)
                    _is_limit = False
                    if po.symbol.startswith(("68", "30")):
                        _is_limit = _pct >= 0.19
                    elif po.symbol.startswith(("4", "8", "92")):
                        _is_limit = _pct >= 0.29
                    else:
                        _is_limit = _pct >= 0.09
                    if _is_limit:
                        self._cancel(po.id, "sealed_limit_up")
                        actions.append({"symbol": po.symbol, "action": "abandon",
                                        "reason": "封死涨停(ask_volume=0), 无法买入"})
                        _log.info(f"[order_manager] ABANDON {po.symbol}: 封死涨停")
                        self._note_signal(day, po.symbol, "abandoned_sealed")
                        continue

            gap = (ask - po.limit_price) / po.limit_price if po.limit_price > 0 else 0

            # ── 时间紧迫度曲线 (线性衰减 09:30→14:50) ──
            _mins_open = max(0, (now.hour - 9) * 60 + now.minute - 30)
            _total_mins = 320  # 09:30→14:50 = 320 min
            _progress = min(1.0, _mins_open / _total_mins)
            urgency = max(RUNAWAY_END, RUNAWAY_START - (RUNAWAY_START - RUNAWAY_END) * _progress)

            if force_now:
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "force_fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] force fill {po.symbol} @¥{ask:.2f}")

            elif ask <= po.limit_price:
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] filled {po.symbol} @¥{ask:.2f} ≤ "
                          f"limit=¥{po.limit_price:.2f}")

            elif gap > urgency:
                self._cancel(po.id, "runaway")
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "abandon_fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] runaway {po.symbol}: "
                          f"gap={gap:+.1%} > urgency={urgency:+.1%}, executing market")

        return actions

    # ── 内部操作 ──
    def _fill(self, po: PendingOrder, price: float, day: str):
        """执行成交: 通过 broker_adapter (ADR-036) 或 engine.execute (回退)."""
        from quant.execution.engine import ExecutionEngine, Order
        from quant.execution.broker_adapter import get_broker_adapter
        cost_model = CostModel.from_config()
        cost_est = cost_model.buy_cost(price, po.target_shares)

        # ADR-036: 尝试通过 broker adapter 下单
        try:
            adapter = get_broker_adapter()
        except Exception:
            adapter = None

        if adapter is not None and adapter.is_connected() and not adapter.name == "simulated":
            # 真实券商路径: 通过 adapter 下单
            result = adapter.buy(po.symbol, price, po.target_shares, order_type="MARKET")
            if not result.success:
                if "insufficient" in str(result.error).lower():
                    _log.warning(f"[order_manager] insufficient cash for {po.symbol}: "
                                 f"{result.error} — cancelling")
                    self._cancel(po.id, "insufficient cash")
                    return
                _log.error(f"[order_manager] broker buy failed: {po.symbol}: {result.error}")
                return
            # 成交成功 — 同步写入 sim_trades (保持 DB 一致)
            try:
                engine = ExecutionEngine()
                engine.execute(
                    [Order(symbol=po.symbol, side="buy", shares=po.target_shares,
                           price=round(price, 2),
                           cost=cost_model.commission(price * po.target_shares))],
                    day, strategy=po.strategy)
            except Exception as _e:
                _log.warning(f"[order_manager] sim_trades sync failed (non-fatal): {_e}")
        else:
            # 模拟路径: engine.execute 直接写 sim_trades
            engine = ExecutionEngine()
            cash = engine.get_cash(po.strategy)
            if cash < cost_est:
                _log.warning(f"[order_manager] insufficient cash for {po.symbol}: "
                             f"need ¥{cost_est:.2f}, have ¥{cash:.2f} — cancelling")
                self._cancel(po.id, "insufficient cash")
                return
            executed = engine.execute(
                [Order(symbol=po.symbol, side="buy", shares=po.target_shares,
                       price=round(price, 2),
                       cost=cost_model.commission(price * po.target_shares))],
                day, strategy=po.strategy)
            if executed == 0:
                _log.warning(f"[order_manager] engine skipped buy for {po.symbol} "
                             f"(ex-dividend?) — cancelling pending order")
                self._cancel(po.id, "engine_skip")
                self._note_signal(day, po.symbol, "engine_skip")
                return
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='filled', filled_at=datetime('now','localtime'), "
            "filled_shares=?, filled_price=? WHERE id=?",
            (po.target_shares, price, po.id))
        c.commit()
        c.close()

        # ── TCA: 记录到达价 vs 成交价滑点 ──
        if po.reference_price and po.reference_price > 0:
            slip_bp = round((price / po.reference_price - 1) * 10000, 1)
            _log.info(f"[order_manager] TCA {po.symbol}: "
                      f"arrival=¥{po.reference_price:.2f} fill=¥{price:.2f} "
                      f"slip={slip_bp:+.1f}bp")

    def _chase(self, order_id: int, new_limit: float):
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET limit_price=?, chase_count=chase_count+1 "
            "WHERE id=?",
            (new_limit, order_id))
        c.commit()
        c.close()

    def _cancel(self, order_id: int, reason: str = ""):
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='cancelled', cancel_reason=? WHERE id=?",
            (reason, order_id))
        c.commit()
        c.close()
        if reason:
            _log.info(f"[order_manager] cancelled order#{order_id}: {reason}")

    def _note_signal(self, day: str, symbol: str, note: str):
        try:
            from quant.data.repos import TradeRepo
            TradeRepo().update_signal_exec_note(day, symbol, note)
        except Exception as e:
            _log.warning(f"[order_manager] exec_note write failed (non-fatal): {e}")

    def cancel_all(self, day: str, strategy: str = "quant"):
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='cancelled' "
            "WHERE day=? AND strategy=? AND status='pending'",
            (day, strategy))
        c.commit()
        c.close()
