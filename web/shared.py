"""线程安全共享状态 — pipeline → Web 的桥接层。

仅做内存缓存，不执行业务逻辑。业务逻辑全在 monitor/ 和 pipeline/ 中。
"""
import threading, sqlite3, os

_lock = threading.Lock()

def _init_state() -> dict:
    """从 trades.db 恢复初始状态。使用 capital_after 列 (执行引擎写入的准确值)。"""
    from config.loader import get as cfg
    state = {"status": "休市", "progress": "",
             "mood": {},
             "signals": [], "sectors": [],
             "summary": {}, "timestamp": ""}
    try:
        db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
        conn = sqlite3.connect(db)
        # 从 capital_after 读取最近一次交易的现金余额 (唯一真相源)
        row = conn.execute(
            "SELECT capital_after FROM sim_trades WHERE strategy='quant' AND capital_after IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            capital = round(row[0], 2)
        else:
            capital = float(cfg("backtest.initial_capital", 5000))
        merged = {}
        for r in conn.execute("""
            SELECT symbol, price, shares, board_count, date FROM sim_trades
            WHERE side='buy' AND strategy='quant' AND symbol NOT IN (
                SELECT symbol FROM sim_trades WHERE side='sell' AND strategy='quant'
            )
        """).fetchall():
            sym, px, sh, board, dt = r[0], r[1], r[2], r[3], r[4]
            if sym in merged:
                m = merged[sym]; total_sh = m["shares"] + sh
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
                       "value": round(m["shares"] * m["price"], 2)}
                     for m in merged.values()]
        pos_value = sum(p["value"] for p in positions)
        state["capital"] = round(capital, 2)
        state["total_asset"] = round(capital + pos_value, 2)
        state["pos_value"] = round(pos_value, 2)
        state["positions"] = positions
        conn.close()
    except Exception:
        state["capital"] = float(cfg("backtest.initial_capital", 5000))
        state["total_asset"] = float(cfg("backtest.initial_capital", 5000))
        state["pos_value"] = 0
        state["positions"] = []
    return state

_state = _init_state()

def get_state() -> dict:
    with _lock: return dict(_state)

def update_state(data: dict):
    with _lock: _state.update(data)
