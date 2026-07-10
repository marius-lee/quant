"""HTTP State Pusher — pipeline → Flask 状态同步 (P69: 从 pipeline.py 抽取).

方案B: pipeline 盘后计算完成后 POST 状态到 Flask，Flask 提供 /api/state 读取。
连续 5 次失败时触发 error 级别日志告警。
"""

import threading
import requests
import numpy as np

from config.loader import get as _cfg
from config.constants import _require_cfg
from utils.logger import get_logger

# ── Failure tracking: alert when consecutive failures exceed threshold ──
_FAIL_COUNT = 0
_FAIL_THRESHOLD = 5
_FAIL_LOCK = threading.Lock()


def _on_fail():
    global _FAIL_COUNT
    with _FAIL_LOCK:
        _FAIL_COUNT += 1
        if _FAIL_COUNT >= _FAIL_THRESHOLD:
            get_logger("state_pusher").error(
                f"post_state failed {_FAIL_COUNT} consecutive times — web may be down"
            )


def _on_success():
    global _FAIL_COUNT
    with _FAIL_LOCK:
        _FAIL_COUNT = 0


def sanitize_for_json(obj):
    """Recursively convert numpy types to Python native types for JSON serialization."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(x) for x in obj]
    return obj


def state_url() -> str:
    port = _require_cfg("web.port")
    return f"http://127.0.0.1:{port}/api/state"


def post_state(data: dict, timeout: float = 5.0, max_retries: int = 3, async_mode: bool = True):
    """POST 状态到 Flask，指数退避重试。

    async_mode=True (默认): fire-and-forget 线程, 不阻塞 pipeline 步骤.
    async_mode=False: 同步模式, 用于测试/调试.
    失败静默 — 不影响 pipeline 执行.
    连续 5 次失败触发 error 日志.
    """
    if async_mode:
        threading.Thread(target=_post_state_sync, args=(data, timeout, max_retries), daemon=True).start()
        return
    _post_state_sync(data, timeout, max_retries)


def _post_state_sync(data: dict, timeout: float, max_retries: int):
    """POST state to Flask. Sanitizes numpy types. Retries only on transient errors."""
    import time as _time
    url = state_url()
    data = sanitize_for_json(data)
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=data, timeout=timeout)
            if r.ok:
                _on_success()
                return
            if 400 <= r.status_code < 500:
                get_logger("state_pusher").warning(f"post_state client error {r.status_code}, not retrying")
                _on_fail()
                return
            get_logger("state_pusher").warning(f"post_state HTTP {r.status_code} (attempt {attempt+1})")
            if attempt == max_retries - 1:
                _on_fail()
        except requests.ConnectionError:
            _on_fail()
            return
        except requests.Timeout:
            get_logger("state_pusher").warning(f"post_state timeout (attempt {attempt+1})")
            if attempt == max_retries - 1:
                _on_fail()
        except requests.RequestException as e:
            get_logger("state_pusher").warning(f"post_state failed: {e} (attempt {attempt+1})")
            if attempt == max_retries - 1:
                _on_fail()
        if attempt < max_retries - 1:
            _time.sleep(2 ** attempt)
