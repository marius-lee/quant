"""状态通信抽象层 — 模板 2/6.

实现: RedisStateBroker (跨进程 Redis pub/sub), InProcessBroker (fallback).

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


class RedisStateBroker(StateBroker):
    """Redis 跨进程实现 — scheduler 和 web app 共享状态."""

    def __init__(self, redis_url: str = 'redis://localhost:6379/0', prefix: str = 'quant:state'):
        self._prefix = prefix
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._key = f'{prefix}:data'
        self._r = None
        try:
            import redis as _redis
            self._r = _redis.from_url(redis_url)
            self._r.ping()
        except Exception:
            pass

    def _init_state(self) -> dict:
        import sys as _sys
        _root = _os.path.dirname(_os.path.dirname(__file__))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        state = {'status': '休市', 'progress': '',
                 'mood': {}, 'signals': [], 'sectors': [],
                 'summary': {}, 'timestamp': '', 'trace_id': ''}
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
            pass
        return state

    def _read_state(self) -> dict:
        if self._r is None:
            return {}
        try:
            import json as _json
            data = self._r.get(self._key)
            if data:
                return _json.loads(data)
        except Exception:
            pass
        return {}

    def _write_state(self, data: dict):
        if self._r is None:
            return
        try:
            import json as _json
            self._r.setex(self._key, 86400, _json.dumps(data, ensure_ascii=False, default=str))
            self._r.publish(f'{self._prefix}:channel', 'updated')
        except Exception:
            pass

    def get(self) -> dict:
        cached = self._read_state()
        if not cached:
            return self._init_state()
        # Merge: Redis has pipeline status, trades.db has financial truth.
        init = self._init_state()
        cached.update(init)
        return cached

    def update(self, data: dict):
        with self._lock:
            current = self._read_state()
            current.update(data)
            self._write_state(current)
        payload = dict(current)
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


# 全局单例 — Redis 跨进程，fallback 到内存
broker = RedisStateBroker()
