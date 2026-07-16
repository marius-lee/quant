"""限价单管理器 — 事件驱动的挂单/追价/补单。

ADR 033: 执行从"09:30 市价一次买入"改为"限价挂单 + 被动成交 + 尾盘补单"。
Monitor 每 5s 调一次 check_and_manage, 不靠定时轮询。

状态机: pending → filled | cancelled → (force_filled)
事件触发:
  A: ask ≤ limit_price → 成交
  B: ask < limit_price 且价差 > chase_threshold → 追价 (上调限价)
  C: ask > limit_price 且价差 > runaway_threshold → 放弃, 市价买入
  D: 时间 ≥ force_fill_time → 全部未成交市价补单
"""
from quant.utils.logger import get_logger
_log = get_logger(__name__)

import sqlite3
from datetime import datetime, time
from dataclasses import dataclass
from typing import Optional
from quant.config.constants import _require_cfg
from quant.config.paths import TRADE_DB


DB_PATH = TRADE_DB

# ── 阈值 (config-driven) ──
DISCOUNT_PCT = _require_cfg("execution.limit_order.discount_pct")
CHASE_THRESHOLD = _require_cfg("execution.limit_order.chase_threshold")
RUNAWAY_THRESHOLD = _require_cfg("execution.limit_order.runaway_threshold")
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
        B: ask < limit && gap > chase_threshold → chase (上调 limit)
        C: ask > limit && gap > runaway_threshold → abandon → market fill
        D: now >= force_fill_time → force fill all

        返回: [{"symbol": ..., "action": "fill/chase/abandon", "shares": ..., "price": ...}, ...]
        """
        now = datetime.now()
        hhmm = time(now.hour, now.minute)
        force_now = hhmm >= FORCE_FILL_TIME

        pending = self.get_pending(day, strategy)
        actions = []

        for po in pending:
            q = quotes.get(po.symbol, {})
            ask = q.get("price", 0) or q.get("open", 0) or 0
            if ask <= 0:
                continue

            gap = (ask - po.limit_price) / po.limit_price if po.limit_price > 0 else 0

            if force_now:
                # 事件 D: 尾盘强制补单
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "force_fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] force fill {po.symbol} @¥{ask:.2f} "
                          f"({po.target_shares}股)")

            elif ask <= po.limit_price:
                # 事件 A: 价格到位 → 成交
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] filled {po.symbol} @¥{ask:.2f} ≤ "
                          f"limit=¥{po.limit_price:.2f}")

            elif gap < -CHASE_THRESHOLD:
                # 事件 B: 当前价比限价低很多 (价格跌了, 可以更便宜买) → 追价下调
                new_limit = round(ask * (1 - DISCOUNT_PCT), 2)
                self._chase(po.id, new_limit)
                actions.append({"symbol": po.symbol, "action": "chase",
                                "old_limit": po.limit_price, "new_limit": new_limit})
                _log.info(f"[order_manager] chase {po.symbol}: "
                          f"¥{po.limit_price:.2f} → ¥{new_limit:.2f} "
                          f"(ask=¥{ask:.2f}, chase#{po.chase_count+1})")

            elif gap > RUNAWAY_THRESHOLD:
                # 事件 C: 价格跑远了 → 取消限价, 市价买入
                self._cancel(po.id, "runaway")
                self._fill(po, ask, day)
                actions.append({"symbol": po.symbol, "action": "abandon_fill",
                                "shares": po.target_shares, "price": ask})
                _log.info(f"[order_manager] runaway {po.symbol}: "
                          f"ask=¥{ask:.2f} vs limit=¥{po.limit_price:.2f} "
                          f"(gap={gap:+.1%}), executing market")

        return actions

    # ── 内部操作 ──
    def _fill(self, po: PendingOrder, price: float, day: str):
        """执行成交: 写入 sim_trades + 更新 pending 状态."""
        from quant.execution.engine import ExecutionEngine, Order
        engine = ExecutionEngine()
        cost_est = round(price * po.target_shares * 1.001 + 5.0, 2)
        cash = engine.get_cash(po.strategy)
        if cash < cost_est:
            _log.warning(f"[order_manager] insufficient cash for {po.symbol}: "
                         f"need ¥{cost_est:.2f}, have ¥{cash:.2f} — cancelling")
            self._cancel(po.id, "insufficient cash")
            return
        engine.execute(
            [Order(symbol=po.symbol, side="buy", shares=po.target_shares,
                   price=round(price, 2), cost=5.0)],
            day, strategy=po.strategy)
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='filled', filled_at=datetime('now'), "
            "filled_shares=?, filled_price=? WHERE id=?",
            (po.target_shares, price, po.id))
        c.commit()
        c.close()

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
            "UPDATE pending_orders SET status='cancelled' WHERE id=?",
            (order_id,))
        c.commit()
        c.close()
        if reason:
            _log.info(f"[order_manager] cancelled order#{order_id}: {reason}")

    def cancel_all(self, day: str, strategy: str = "quant"):
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='cancelled' "
            "WHERE day=? AND strategy=? AND status='pending'",
            (day, strategy))
        c.commit()
        c.close()
