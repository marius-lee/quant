"""共享数据库操作 — auto_run.py 和 app.py 共用"""
import json, sqlite3, os
from datetime import datetime

import numpy as np
import pandas as pd

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "results.db")


def _to_native(obj):
    """递归转换 numpy/pandas 类型为 Python 原生类型，确保 JSON 可序列化。"""
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(v) for v in obj]
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.bool_,)): return bool(obj)
    if isinstance(obj, (pd.Timestamp,)): return str(obj)
    if isinstance(obj, (np.ndarray,)): return _to_native(obj.tolist())
    return obj


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
            n_stocks INTEGER, n_days INTEGER, sharpe REAL,
            annual_return REAL, max_drawdown REAL, win_rate REAL, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER REFERENCES runs(id),
            rank INTEGER, symbol TEXT, name TEXT, score REAL, price REAL
        );
    """)
    conn.commit()
    conn.close()


def save_result(result: dict):
    if "metrics" not in result:
        return
    conn = sqlite3.connect(DB_PATH)
    m = result["metrics"]
    cur = conn.execute(
        """INSERT INTO runs (run_at, n_stocks, n_days, sharpe, annual_return, max_drawdown, win_rate, raw_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (datetime.now().isoformat(), result["n_stocks"], result["n_days"],
         m.get("sharpe_ratio"), m.get("annual_return"),
         m.get("max_drawdown"), m.get("win_rate"), json.dumps(_to_native(result), ensure_ascii=False))
    )
    run_id = cur.lastrowid
    for i, rec in enumerate(result["recommendations"]):
        conn.execute(
            "INSERT INTO picks (run_id, rank, symbol, name, score, price) VALUES (?,?,?,?,?,?)",
            (run_id, i + 1, rec["symbol"], rec.get("name", ""), rec["score"], rec["last_price"])
        )
    conn.commit()
    conn.close()

    # 模拟交易: 推荐 → 自动买入
    try:
        from engine.sim_broker import init_simulation, execute_simulation
        init_simulation()
        execute_simulation(result)
    except Exception:
        pass


def get_history(limit: int = 5) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in runs:
        run_data = dict(r)
        picks = conn.execute("SELECT * FROM picks WHERE run_id=? ORDER BY rank", (r["id"],)).fetchall()
        run_data["picks"] = [dict(p) for p in picks]
        result.append(run_data)
    conn.close()
    return result
