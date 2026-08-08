"""XGBoost 模型训练调度器 — v421: XGBoost 接入 (与 lgb_train 对称).

夜间因子物化完成后与 LightGBM 同步重训 XGB 模型 (周一/周四)。
训练窗口: 可配置 alpha.xgb.train.start_date。
"""

import time as _time, uuid as _uuid
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("xgb_train", today, grace_seconds=3600)
    if rid is None:
        _log.info(f"[{today}] xgb_train already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] nightly XGB model training")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:
        from quant.alpha.xgb_model import train_xgb_model, _check_xgboost

        if not _check_xgboost():
            _log.info(f"[{today}] xgboost not installed, skipping XGB training")
            status = "skipped"
            summary = {"reason": "xgboost not installed"}
            _m.inc("scheduler.xgb_train.skip")
            return

        meta = train_xgb_model(factor_status_filter="backtesting")
        elapsed = _time.time() - t0
        _log.info(f"[{today}] xgb_train done: IC={meta.ic_mean:.4f}, "
                  f"{meta.n_samples} samples × {meta.n_features} features ({elapsed:.1f}s)")
        status = "ok"
        summary = {
            "ic_mean": meta.ic_mean, "n_samples": meta.n_samples,
            "n_features": meta.n_features, "elapsed": round(elapsed, 1),
        }
        _m.inc("scheduler.xgb_train.ok")

    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] xgb_train crashed: {e}")
        _m.inc("scheduler.xgb_train.fail")
        raise
    finally:
        _tk_finish("xgb_train", today, status, error=error_msg, summary=summary)
