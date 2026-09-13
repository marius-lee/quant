"""Web routes — core group."""
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from web.app import app, _require_token, _api_response, _require_cfg, cfg, logger, VERSION
from web.services import PositionService, BacktestService, StockService, SignalService
from web.shared import get_state, update_state
from quant.data.repos import TradeRepo
from quant.config.constants import _require_cfg as _rcf
import json

@app.get("/")
def index(request: Request):
    """首页 — 传递 perf 数据供服务端渲染仪表盘。"""
    from web.app import templates
    try:
        from quant.data.repos import TradeRepo
        repo = TradeRepo()
        _strategy = _require_cfg("strategy.name")
        base = repo.get_initial_capital(_strategy)
        capital = repo.get_cash(_strategy) or base
        position_cost = repo.get_open_position_cost(_strategy)
        # P2-5 fix: SQL 封装到 PositionService, 使用 config strategy.name 替代硬码 "quant"
        position_value = PositionService.estimate_position_value(_strategy) or position_cost
        total_asset = round(capital + position_value, 2)
        total_pnl = round(total_asset - base, 2)
        perf = {"total_pnl": total_pnl, "total_asset": total_asset, "initial_capital": base}
    except Exception:
        perf = {"total_pnl": 0, "total_asset": 5000, "initial_capital": 5000}
    from quant.scheduler import get_orchestrator_mode
    return templates.TemplateResponse(request, "index.html", {"version": VERSION, "perf": perf, "orchestrator_mode": get_orchestrator_mode()})


@app.get("/api/state")
def api_state():
    """当前完整状态 (模板6: {data, error} 信封): 资金 + 持仓 + 信号 + 暴露"""
    return get_state()


@app.post("/api/curator/submit")
async def api_curator_submit(request: Request):
    """因子策展提交 — 手动添加因子到内置库."""
    try:
        _auth = _require_token(request)
        if _auth:
            return _auth
        body = await request.json()
        name = body.get("name", "").strip()
        expression = body.get("expression", "").strip()
        source = body.get("source", "").strip()
        direction = body.get("direction", "positive")
        if not name or not expression or not source:
            return _api_response(error={"code": "INVALID", "message": "name/expression/source required"}, status_code=400)
        from quant.factor.factor_curator import submit_factor
        result = submit_factor(name, expression, source, direction=direction)
        return _api_response(data=result)
    except Exception as e:
        logger.warning(f"curator submit failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": str(e)},status_code=500)


@app.get("/api/lgb")
def api_lgb():
    """LightGBM 模型状态与最新预测 (ADR-037 改进项)."""
    try:
        from quant.alpha.qlib_model import get_lgb_model, _check_lightgbm
        lgb_available = _check_lightgbm()
        models = []
        is_trained = False
        metadata = None
        latest_pred = None

        if lgb_available:
            model = get_lgb_model(auto_load=True)
            is_trained = model.is_trained
            models = model.list_models()
            if model.metadata:
                metadata = {
                    "ic_mean": model.metadata.ic_mean,
                    "n_samples": model.metadata.n_samples,
                    "n_features": model.metadata.n_features,
                    "train_date": model.metadata.train_date,
                    "train_start": model.metadata.train_start,
                    "train_end": model.metadata.train_end,
                    "oos_ic_mean": model.metadata.oos_ic_mean,
                    "oos_ic_std": model.metadata.oos_ic_std,
                    "oos_icir": model.metadata.oos_icir,
                    "oos_n_days": model.metadata.oos_n_days,
                    "feature_names": model.metadata.feature_names[:10],
                }

        combine_mode = cfg("alpha.combine_mode", default="sleeve")
        return _api_response(data={
            "available": lgb_available,
            "trained": is_trained,
            "enabled": combine_mode == "lgb",
            "combine_mode": combine_mode,
            "models": models[-5:],  # last 5 models
            "metadata": metadata,
        })
    except Exception as e:
        return _api_response(error=str(e))


@app.get("/api/xgb")
def api_xgb():
    """XGBoost 模型状态与最新预测 (v421: XGBoost 接入)."""
    try:
        from quant.alpha.xgb_model import get_xgb_model, _check_xgboost
        xgb_available = _check_xgboost()
        models = []
        is_trained = False
        metadata = None

        if xgb_available:
            model = get_xgb_model(auto_load=True)
            is_trained = model.is_trained
            models = model.list_models()
            if model.metadata:
                metadata = {
                    "ic_mean": model.metadata.ic_mean,
                    "n_samples": model.metadata.n_samples,
                    "n_features": model.metadata.n_features,
                    "train_date": model.metadata.train_date,
                    "train_start": model.metadata.train_start,
                    "train_end": model.metadata.train_end,
                    "oos_ic_mean": model.metadata.oos_ic_mean,
                    "oos_ic_std": model.metadata.oos_ic_std,
                    "oos_icir": model.metadata.oos_icir,
                    "oos_n_days": model.metadata.oos_n_days,
                    "feature_names": model.metadata.feature_names[:10],
                }

        combine_mode = cfg("alpha.combine_mode", default="sleeve")
        return _api_response(data={
            "available": xgb_available,
            "trained": is_trained,
            "enabled": combine_mode == "xgb",
            "combine_mode": combine_mode,
            "models": models[-5:],  # last 5 models
            "metadata": metadata,
        })
    except Exception as e:
        return _api_response(error=str(e))


@app.get("/api/signals/quality")
def api_signals_quality():
    """信号质量对比 — 今日信号 vs 历史信号统计 (ADR-037 改进项)."""
    try:
        from quant.data.repos import TradeRepo
        repo = TradeRepo()
        today = datetime.now().strftime("%Y-%m-%d")
        sig_today = repo.get_latest_signals()
        today_targets = sig_today.get("targets", []) if sig_today else []
        today_date = sig_today.get("date", "") if sig_today else ""

        # P2-5 fix: SQL 封装到 SignalService
        rows = SignalService.get_recent_signals(limit=20)

        import json
        hist_counts = []
        hist_scores = []
        for row in rows:
            d, js = row["date"], row["signals"]
            try:
                targets = json.loads(js) if isinstance(js, str) else js
                hist_counts.append(len(targets))
                for t in targets:
                    if isinstance(t, dict) and t.get("score"):
                        hist_scores.append(float(t["score"]))
            except (json.JSONDecodeError, TypeError):
                pass

        today_count = len(today_targets)
        today_scores = [t.get("score", 0) for t in today_targets if t.get("score")]
        today_avg_score = sum(today_scores) / max(len(today_scores), 1)
        hist_avg_count = sum(hist_counts) / max(len(hist_counts), 1)
        hist_avg_score = sum(hist_scores) / max(len(hist_scores), 1)

        return _api_response(data={
            "today": {
                "date": today_date,
                "count": today_count,
                "avg_score": round(today_avg_score, 4),
                "max_score": round(max(today_scores), 4) if today_scores else 0,
            },
            "historical": {
                "avg_count": round(hist_avg_count, 1),
                "avg_score": round(hist_avg_score, 4),
                "n_days": len(hist_counts),
            },
            "comparison": {
                "count_pct": round((today_count / max(hist_avg_count, 1) - 1) * 100, 1),
                "score_diff": round(today_avg_score - hist_avg_score, 4),
            },
        })
    except Exception as e:
        return _api_response(error=str(e))


@app.get("/api/backtest/history")
def api_backtest_history():
    """回测历史记录 — backtest_runs 表 (P2-5: SQL 封装到 BacktestService)."""
    try:
        result = BacktestService.get_history(limit=20)
        return _api_response(data=result)
    except Exception as e:
        logger.warning(f"backtest history failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": "backtest history 查询失败"}, status_code=500)


@app.get("/api/factors")
def api_factors(request: Request):
    """因子统一视图 (v505 合并: 原评估线 + 原因子平台线).

    返回:
      - registry: factor_registry (market.db, 唯一真相源) 全因子元数据 + 血缘
      - state: 状态机分布 (active/probation/evaluating/archived)
      - stats: IC/IR/衰减/相关性 (factor_snapshot, 24h TTL)
    ?refresh=true 强制重算统计快照。
    """
    from quant.factor.stats_cache import get_cached_factor_stats
    from web.admin_services import factor_platform_snapshot
    force = request.query_params.get("refresh", "false").lower() == "true"
    try:
        stats = get_cached_factor_stats(force_refresh=force)
        platform = factor_platform_snapshot()
        dist = {item["status"]: 0 for item in platform["factors"]}
        for item in platform["factors"]:
            dist[item["status"]] = dist.get(item["status"], 0) + 1
        registry = platform["factors"]
        return _api_response(data={
            "registry": registry,
            "state": platform["state"],
            "counts": platform["counts"],
            "n_total": len(registry),
            "n_active": dist.get("active", 0),
            "n_probation": dist.get("probation", 0),
            "n_evaluating": dist.get("evaluating", 0),
            "n_archived": dist.get("archived", 0),
            "n_evaluated": sum(1 for f in registry if f.get("ic_mean") is not None),
            "ic": stats.get("ic", []),
            "ic_ir": stats.get("ic_ir", []),
            "factor_keys": stats.get("factor_keys", []),
            "decay": stats.get("decay", {}),
            "corr": stats.get("corr", []),
            "meta": stats.get("meta", {}),
            "cached_at": stats.get("cached_at"),
        })
    except Exception as e:
        from quant.utils.logger import get_logger
        get_logger("web.app").warning(f"Factor unified failed: {e}", exc_info=True)
        return _api_response(error={"code": "FACTOR_ERROR", "message": str(e)})


