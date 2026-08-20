"""全局数据库路径常量 — 单一真相源，整个项目从此 import。

数据层两域划分 (报告 §6.3, 2026-07-26 落地):
  账务域 (SQLite, 写密集/事务): trades.db / backtest_trades.db
    — owner: data/repos (TradeRepo), scheduler/order_manager+reconcile 同域直读
  行情因子域 (读密集/大表): market.db / factor_cache.db
    — owner: data/store (DataStore/market_conn), factor/store (FactorStore)
  监控域: metrics.db — owner: monitor/metrics (自闭环)
  跨域只读只允许经 repo/store 层 (如 attribution 读 trades+market);
  禁止业务代码 sqlite3.connect 跨库应用层 join.
  已清理僵尸: sim_trades.db / quant.db / factor.db (0B 无引用),
  benchmark.db (空表, 基准数据在 market.db), QUANT_DB/BENCHMARK_DB 常量.
"""
import os as _os

_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
DATA_DIR = _os.path.join(_PROJECT_ROOT, "quant", "data")

# ── 账务域 ──
TRADE_DB     = _os.path.join(DATA_DIR, "trades.db")
BACKTEST_DB   = _os.path.join(DATA_DIR, "backtest_trades.db")

# ── 行情因子域 ──
MARKET_DB     = _os.path.join(DATA_DIR, "market.db")
FACTOR_CACHE_DB = _os.path.join(DATA_DIR, "factor_cache.db")

# ── 监控域 ──
METRICS_DB    = _os.path.join(DATA_DIR, "metrics.db")

# ── 日志目录 ──
LOGS_DIR = _os.path.join(_PROJECT_ROOT, "logs")

# ── 其他数据文件 ──
TRADE_CALENDAR = _os.path.join(DATA_DIR, "trade_calendar.json")
OPTUNA_DIR     = DATA_DIR

# ── 便捷：判断存在 ──
def exists(path: str) -> bool:
    return _os.path.exists(path)

# ── MLflow / BentoML 路径 (v436 Phase 3) ──
MLFLOW_TRACKING_URI = _os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
BENTOML_STORE_PATH = _os.path.join(DATA_DIR, "bentoml")

# ── MLflow / BentoML 路径 (v436 Phase 3) ──
MLFLOW_TRACKING_URI = _os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
BENTOML_STORE_PATH = _os.path.join(DATA_DIR, "bentoml")
