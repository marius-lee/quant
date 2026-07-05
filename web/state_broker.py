"""状态通信抽象层 — 模板 2/6.

当前实现: InProcessBroker (线程安全内存 + SSE 广播队列)
未来可换: RedisBroker (pub/sub, 多进程友好)

接口:
  broker.get()       → dict   # 获取当前状态
  broker.update(d)   → None   # 更新状态 + 广播 SSE
  broker.subscribe() → Queue  # SSE 客户端订阅
  broker.unsubscribe(q)       # SSE 客户端取消
"""
import threading, queue
import os as _os
from abc import ABC, abstractmethod


class StateBroker(ABC):
    @abstractmethod
    def get(self) -> dict: ...
    @abstractmethod
    def update(self, data: dict): ...
    @abstractmethod
    def subscribe(self) -> queue.Queue: ...
    @abstractmethod
    def unsubscribe(self, q: queue.Queue): ...


class InProcessBroker(StateBroker):
    """Flask 进程内实现: Lock + 队列广播 (SSE)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = self._init_state()
        self._clients: list[queue.Queue] = []

    def _init_state(self) -> dict:
        """从 trades.db 恢复初始状态."""
        import sys
        _root = _os.path.dirname(_os.path.dirname(__file__))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        state = {"status": "休市", "progress": "",
                 "mood": {}, "signals": [], "sectors": [],
                 "summary": {}, "timestamp": ""}
        try:
            from data.trade_repo import TradeRepo
            db = _os.path.join(_root, "data", "trades.db")
            repo = TradeRepo(db)
            capital = repo.get_cash("quant")
            raw_positions = repo.get_positions("quant")
            positions = []
            for p in raw_positions:
                positions.append({
                    "symbol": p["symbol"], "name": "",
                    "shares": p["shares"], "price": p.get("price", 0),
                    "board_count": p.get("board_count", 0),
                    "date": p.get("date", ""),
                    "current": p.get("price", 0), "pnl_pct": 0,
                    "value": round(p["shares"] * p.get("price", 0), 2)
                })
            pos_value = sum(p["value"] for p in positions)
            state["capital"] = round(capital, 2)
            state["total_asset"] = round(capital + pos_value, 2)
            state["pos_value"] = round(pos_value, 2)
            state["positions"] = positions
        except Exception:
            from data.trade_repo import TradeRepo
            base = float(TradeRepo().get_initial_capital("quant") or 5000)
            state["capital"] = base
            state["total_asset"] = base
            state["pos_value"] = 0
            state["positions"] = []
        return state

    def get(self) -> dict:
        with self._lock:
            return dict(self._state)

    def update(self, data: dict):
        with self._lock:
            self._state.update(data)
        # SSE 广播
        payload = dict(self._state)
        dead = []
        for q in self._clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._clients.remove(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10)
        self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        try:
            self._clients.remove(q)
        except ValueError:
            pass


# 全局单例
broker = InProcessBroker()
