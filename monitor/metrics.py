"""本地轻量指标系统 (模板9 T1) — 替代 Prometheus.

线程安全内存计数 + gauge + 定期 SQLite 落盘.
"""

import threading
import time
import sqlite3
import os as _os

_DB = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "metrics.db")


class Metrics:
    """单例指标收集器. 用法:

        from monitor.metrics import metrics
        metrics.inc('pipeline.runs')
        metrics.gauge('pipeline.last_duration_s', 42.5)
        metrics.snapshot()  # -> {counters: {...}, gauges: {...}}
    """

    _instance = None

    def __init__(self, db_path: str = _DB):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._db = db_path
        self._ensure_db()

    def _ensure_db(self):
        conn = sqlite3.connect(self._db)
        conn.execute("""CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, type TEXT NOT NULL,
            value REAL NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
        conn.close()

    def inc(self, name: str, delta: int = 1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + delta

    def gauge(self, name: str, value: float):
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }

    def persist(self):
        """落盘当前指标到 SQLite. scheduler 每次循环调用."""
        snap = self.snapshot()
        conn = sqlite3.connect(self._db)
        for name, val in snap["counters"].items():
            conn.execute("INSERT INTO metrics (name,type,value) VALUES (?,?,?)",
                         (name, "counter", val))
        for name, val in snap["gauges"].items():
            conn.execute("INSERT INTO metrics (name,type,value) VALUES (?,?,?)",
                         (name, "gauge", val))
        conn.commit()
        conn.close()

    def reset_counters(self):
        """重置计数器 (保留 gauge)."""
        with self._lock:
            self._counters.clear()


# 全局单例
metrics = Metrics()
