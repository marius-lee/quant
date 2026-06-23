"""共享数据库连接 — 陈小群体系共用 (paper/sim_broker/tracker)。"""
import sqlite3, os

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "results.db")

_conn = None


def get_conn():
    """返回 results.db 共享连接。check_same_thread=False 允许多线程访问。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def close():
    """关闭 results.db 连接。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
