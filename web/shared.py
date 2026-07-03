"""线程安全共享状态 — pipeline → Web 的桥接层。

仅做内存缓存，不执行业务逻辑。业务逻辑全在 monitor/ 和 pipeline/ 中。
"""
import threading
import os

_lock = threading.Lock()

def _init_state() -> dict:
    """从 trades.db 恢复初始状态 — 委托 TradeRepo。"""
    import sys, os as _os
    _root = _os.path.dirname(_os.path.dirname(__file__))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from data.trade_repo import TradeRepo
    from config.loader import get as cfg

    state = {"status": "休市", "progress": "",
             "mood": {}, "signals": [], "sectors": [],
             "summary": {}, "timestamp": ""}
    try:
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
        from data.trade_repo import TradeRepo; state["capital"] = float(TradeRepo().get_initial_capital("quant") or 5000)
        state["total_asset"] = float(TradeRepo().get_initial_capital("quant") or 5000)
        state["pos_value"] = 0
        state["positions"] = []
    return state

_state = _init_state()

def get_state() -> dict:
    with _lock: return dict(_state)

def update_state(data: dict):
    with _lock: _state.update(data)
