"""量化选股 Web — 7 层架构监控仪表盘。

状态: web/shared.py 内存共享 (pipeline 写入, Flask 读取)
持久: quant/data/trades.db (交易唯一真相源)
"""

import json, os, sqlite3
from quant.config.constants import _require_cfg

from quant.utils.excepthook import setup; setup()
from quant.config.paths import TRADE_DB, MARKET_DB, BACKTEST_DB  # crash → app.log
from web.services import PositionService, BacktestService, StockService, SignalService  # P2-5
from quant.config.loader import get as cfg, validate; validate()  # 启动时校验 config.yaml 类型
from quant.data.store import market_conn  # P69: 统一连接层
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# v562 fix: configurable current date for testing
_test_current_date = None

def get_current_date():
    """Get current date, overridable for testing."""
    global _test_current_date
    if _test_current_date is not None:
        return _test_current_date
    return date.today()

def set_test_current_date(d: date):
    """Set current date for testing."""
    global _test_current_date
    _test_current_date = d

# 前端版本标识 — 修改此处触发浏览器刷新认知
VERSION = "test-v646"
# ── 进程退出埋点 ──
import atexit as _atexit, signal as _signal, sys as _sys, threading as _thr, os as _os

def _log_exit(reason: str = ""):
    try:
        from quant.utils.logger import get_logger
        get_logger("web.app").warning(
            f"EXIT | reason={reason or 'unknown'} | pid={os.getpid()} | "
            f"thread={_thr.current_thread().name}")
    except Exception:
        print(f"[EXIT] {reason} pid={os.getpid()}", flush=True)

def _clean_exit(reason: str):
    """P78: ThreadPoolExecutor 线程随 with 语句自动回收, 无需手动清理."""
    _log_exit(reason)
    _sys.exit(0)

_atexit.register(_log_exit, "atexit")
_signal.signal(_signal.SIGTERM, lambda s, f: _clean_exit("SIGTERM"))
_signal.signal(_signal.SIGINT,  lambda s, f: _clean_exit("SIGINT"))

from quant.utils.logger import get_logger

logger = get_logger("web.app")

app = FastAPI(title="Quant Web", version=VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="web/templates")
app.mount("/static", StaticFiles(directory="web/static"), name="static")
_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"


def _api_response(data=None, *, meta=None, error=None, status_code: int = 200):
    """模板 6: 统一 API 响应信封 {data, meta, error}.
    error 格式: {"code": "ERROR_CODE", "message": "人类可读描述", "details": [...]} (可选)
    """
    body = {"data": data, "meta": meta, "error": error}
    if status_code == 200:
        return body
    return JSONResponse(status_code=status_code, content=body)


def _require_token(request: Request):
    """C14 (CODE-REVIEW): 写操作统一鉴权入口 — 设置 QUANT_API_TOKEN 后,
    所有 POST 必须带 X-API-Token 头 (hmac 比较防时序侧信道).

    返回 None 表示通过; 否则返回 (response, status) 供路由直接返回.
    v625: 未配置 QUANT_API_TOKEN 时记录警告, 提醒开启鉴权.
    """
    import hmac as _hmac
    _token = os.environ.get("QUANT_API_TOKEN")
    if not _token:
        logger.warning("_require_token: QUANT_API_TOKEN 未设置, 写操作无鉴权 (建议配置)")
        return None
    _given = request.headers.get("X-API-Token", "")
    if not _hmac.compare_digest(_given, _token):
        return JSONResponse(status_code=401, content={"data": None, "error": {"code": "UNAUTHORIZED", "message": "missing or invalid X-API-Token"}})
    return None

# 启动时异步预热因子评估缓存 (首次 /api/factors 请求免等待)
import threading
# 缓存预热已移除 — web 启动不应触发因子计算, 首次 API 请求时懒加载


def _capital(strategy: str) -> float:
    """从 strategy_config 表读本金。无记录时默认 5000 并自动写入。"""
    from quant.data.repos import TradeRepo
    repo = TradeRepo()
    cap = repo.get_initial_capital(strategy)
    if cap <= 0:
        from quant.config.constants import _require_cfg as _rcf
        cap = float(_rcf("live.default_capital"))
        repo.set_initial_capital(strategy, cap)
    return cap

from quant.core.state_broker import broker
from web.shared import get_state, update_state  # deprecated, kept for compat


# ═══════════════════════════════════════════════════════════

from web.routes import *

if __name__ == "__main__":
    import uvicorn
    port = int(_require_cfg("web.port"))
    from quant.monitoring.prometheus import init_monitoring
    init_monitoring()  # 启动 MetricsCollector (30s 系统指标 + 低频行数) — 模板 9
    logger.info(f"Web 服务启动于端口 {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
