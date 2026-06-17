"""线程安全共享状态 — 替代 trading_state.json 文件IPC。

intraday_runner 写入瞬态, Flask 读取。
持久化数据(持仓/交易)走 trades.db。
启动时从 DB 初始化，不写死默认值。
"""

import threading, sqlite3, os

_lock = threading.Lock()

def _init_state() -> dict:
    """从 trades.db 初始化状态(单次调用)"""
    from config.loader import get as cfg
    state = {
        "status": "休市", "progress": "", "mood": {},
        "golden_signals": [], "final_signals": [],
        "all_signals": [], "sectors": [], "summary": {}, "timestamp": "",
    }
    base_capital = float(cfg("backtest.initial_capital", 5000))
    try:
        db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
        conn = sqlite3.connect(db)
        capital = base_capital
        for side, price, shares in conn.execute("SELECT side, price, shares FROM sim_trades ORDER BY id").fetchall():
            val = price * shares
            if side == "buy":
                capital -= val + max(val * 0.0003, 5)
            else:
                capital += val - max(val * 0.0003, 5) - val * 0.001
        # 持仓 (合并同股票多笔买入, 加权平均成本)
        merged = {}
        for r in conn.execute("""
            SELECT symbol, price, shares, board_count, date FROM sim_trades
            WHERE side='buy' AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell')
        """).fetchall():
            sym, px, sh, board, dt = r[0], r[1], r[2], r[3], r[4]
            if sym in merged:
                m = merged[sym]
                total_sh = m["shares"] + sh
                m["price"] = round((m["price"] * m["shares"] + px * sh) / total_sh, 2)
                m["shares"] = total_sh
                m["board_count"] = max(m["board_count"], board)
                m["date"] = min(m["date"], dt)
            else:
                merged[sym] = {"symbol": sym, "shares": sh, "price": px,
                               "board_count": board, "date": dt}
        positions = [{"symbol": m["symbol"], "name": "", "shares": m["shares"],
                       "price": m["price"], "board_count": m["board_count"],
                       "date": m["date"], "current": m["price"], "pnl_pct": 0,
                       "value": round(m["shares"] * m["price"], 2),}
                     for m in merged.values()]
        pos_value = sum(p["value"] for p in positions)
        state["capital"] = round(capital, 2)
        state["total_asset"] = round(capital + pos_value, 2)
        state["pos_value"] = round(pos_value, 2)
        state["positions"] = positions
        conn.close()
    except Exception:
        state["capital"] = base_capital
        state["total_asset"] = base_capital
        state["pos_value"] = 0
        state["positions"] = []
    return state

_state = _init_state()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def update_state(data: dict):
    with _lock:
        _state.update(data)
