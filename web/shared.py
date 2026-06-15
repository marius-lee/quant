"""线程安全共享状态 — 替代 trading_state.json 文件IPC。

intraday_runner 写入瞬态, Flask 读取。
持久化数据(持仓/交易)走 trades.db。
"""

import threading

_lock = threading.Lock()
_state = {
    "status": "休市",
    "progress": "",
    "capital": 5000.0,
    "total_asset": 5000.0,
    "pos_value": 0,
    "positions": [],
    "mood": {},
    "golden_signals": [],
    "final_signals": [],
    "all_signals": [],
    "sectors": [],
    "summary": {},
    "timestamp": "",
}


def get_state() -> dict:
    with _lock:
        return dict(_state)


def update_state(data: dict):
    with _lock:
        _state.update(data)
