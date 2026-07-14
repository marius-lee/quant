"""全局数据库路径常量 — 单一真相源，整个项目从此 import。"""
import os as _os

_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
DATA_DIR = _os.path.join(_PROJECT_ROOT, "quant", "data")

# ── DB 文件 ──
TRADE_DB     = _os.path.join(DATA_DIR, "trades.db")
MARKET_DB     = _os.path.join(DATA_DIR, "market.db")
BACKTEST_DB   = _os.path.join(DATA_DIR, "backtest_trades.db")
BENCHMARK_DB  = _os.path.join(DATA_DIR, "benchmark.db")
METRICS_DB    = _os.path.join(DATA_DIR, "metrics.db")
QUANT_DB      = _os.path.join(DATA_DIR, "quant.db")

# ── 其他数据文件 ──
TRADE_CALENDAR = _os.path.join(DATA_DIR, "trade_calendar.json")
OPTUNA_DIR     = DATA_DIR

# ── 便捷：判断存在 ──
def exists(path: str) -> bool:
    return _os.path.exists(path)
