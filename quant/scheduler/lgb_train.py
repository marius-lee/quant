"""LightGBM 模型训练调度器 — ADR-037 改进项.

每日夜间因子物化完成后自动重训 LGB 模型。
训练窗口: 最近 500 个交易日 (可配置 alpha.lgb.train.start_date)。
"""

import time as _time, uuid as _uuid
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.scheduler.manifest import EVENING_STAGE_GRACE
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("lgb_train", today, grace_seconds=EVENING_STAGE_GRACE["lgb_train"])
    if rid is None:
        _log.info(f"[{today}] lgb_train already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] nightly LGB model training")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:
        from quant.alpha.qlib_model import train_lgb_model, _check_lightgbm

        if not _check_lightgbm():
            _log.info(f"[{today}] lightgbm not installed, skipping LGB training")
            status = "skipped"
            summary = {"reason": "lightgbm not installed"}
            _m.inc("scheduler.lgb_train.skip")
            return

        meta = train_lgb_model(factor_status_filter="backtesting")
        elapsed = _time.time() - t0
        _log.info(f"[{today}] lgb_train done: IC={meta.ic_mean:.4f}, "
                  f"{meta.n_samples} samples × {meta.n_features} features ({elapsed:.1f}s)")
        status = "ok"
        summary = {
            "ic_mean": meta.ic_mean, "n_samples": meta.n_samples,
            "n_features": meta.n_features, "elapsed": round(elapsed, 1),
        }
        _m.inc("scheduler.lgb_train.ok")

    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] lgb_train crashed: {e}")
        _m.inc("scheduler.lgb_train.fail")
        raise
    finally:
        _tk_finish("lgb_train", today, status, error=error_msg, summary=summary)
