"""管理后台服务层 (v502) — 因子平台 / 多策略 / 另类数据 / 分布式回测 / 模型服务.

与 web/services.py 同约定: Router 只做 request → service → response.
本模块封装的是「管理类」后端能力 (非 SQL 型), 避免把业务逻辑塞进 app.py 路由.
"""

import json
import threading
from typing import Optional

from quant.utils.logger import get_logger

logger = get_logger("web.admin_services")


# ═══════════════════════════════════════════════════════════
# 因子平台 (quant.factor.platform)
# ═══════════════════════════════════════════════════════════

# 基础数据列 (血缘上游 token 匹配)
_BASE_COLUMNS = frozenset({
    "close", "open", "high", "low", "volume", "amount", "turnover",
    "vwap", "pre_close", "adj_factor", "ret", "pct_chg", "limit_up",
    "limit_down", "bid1", "ask1",
})
# 因子托管族: compute_fn 前缀 / 函数归属 → 数据族标签
_FAMILY_LABELS = {
    "compute_": "daily 行情衍生 (price)",
    "macro_": "宏观数据 (macro)",
    "act_": "股东行为 (activist)",
    "fundamental": "财务状况 (fundamental)",
}
# 函数名 → 数据族 (血缘回退标签)
_FN_FAMILY = {
    "compute_reversal": "ohlcv 行情",
    "compute_turnover_reversal": "ohlcv 行情",
    "compute_max_return": "ohlcv 行情",
    "compute_overnight_gap": "ohlcv 行情",
    "compute_intraday_range": "ohlcv 行情",
    "compute_momentum": "ohlcv 行情",
    "compute_residual_momentum": "ohlcv 行情",
    "compute_volatility": "ohlcv 行情",
    "compute_skewness": "ohlcv 行情",
    "compute_idiosyncratic_vol": "ohlcv 行情",
    "compute_amihud": "ohlcv 行情",
    "compute_amihud_20d": "ohlcv 行情",
    "compute_turnover_adj_amihud": "ohlcv 行情",
    "compute_rsi_reversal": "ohlcv 行情",
    "compute_money_flow": "ohlcv 行情",
    "compute_ma_alignment": "ohlcv 行情",
    "compute_volume_price_corr": "ohlcv 行情",
    "compute_turnover_anomaly": "ohlcv 行情",
    "compute_limit_up_proximity": "ohlcv 行情",
    "compute_limit_up_streak": "ohlcv 行情",
    "compute_dt_streak": "ohlcv 行情",
    "compute_lhb_net_buy": "龙虎榜 (lhb)",
    "compute_zjw": "资金流 (zjw)",
}


def _derive_lineage(compute_fn: str, formula: str,
                    all_fns: dict) -> dict:
    """由 compute_fn/formula 推导血缘.

    upstream: compute_fn 为函数名 → map 归属族标签;
              为表达式 → 提取基础数据 token 列
    downstream: 扫描其他因子的 compute_fn/formula 引用本因子名
    """
    import re
    upstream, downstream = [], []

    src = (compute_fn or "") + " " + (formula or "")

    # 表达式形式 (含 ts_mean/ts_delay 等) → 提取数据列 token
    if re.search(r"[a-z_]+\(.*\)", src):
        cols = sorted({tok for tok in re.findall(r"\b[a-z_][a-z_0-9]*\b", src)
                       if tok in _BASE_COLUMNS})
        if cols:
            upstream.append({
                "type": "data", "ref": ",".join(cols),
                "label": f"行情列: {cols[0]}" + (f" 等 {len(cols)} 列" if len(cols) > 1 else ""),
            })
    else:
        # 纯函数名 → 归属族回退
        fam = _FN_FAMILY.get(compute_fn or "", "")
        if not fam:
            for prefix, label in _FAMILY_LABELS.items():
                if (compute_fn or "").startswith(prefix):
                    fam = label
                    break
        if fam:
            upstream.append({"type": "family", "ref": compute_fn,
                             "label": f"{fam} (计算: {compute_fn})"})

    # 其他因子表达式引用本因子名 (词边界匹配, 防子串误配如 compute_dt_streak ⊃ dt_streak)
    for other in all_fns:
        if other == compute_fn:
            continue
        if re.search(rf"\b{re.escape(other)}\b", compute_fn or "") or \
           re.search(rf"\b{re.escape(other)}\b", formula or ""):
            downstream.append(other)
    return {"upstream": upstream, "downstream": sorted(set(downstream))}


def factor_platform_snapshot() -> dict:
    """因子平台总览: 注册表全量 (market.db factor_registry, 唯一真相源)
    + 状态机分布 + 血缘推导.

    v505: 废弃 factor_metadata (factor_registry.db) 空壳线 — 此前读它=空表,
    平台页是摆设. 该表从未被填充, 真实驱动数据在 factor_registry (market.db).
    """
    from quant.data.repos import FactorRepo

    repo = FactorRepo()
    rows = repo.get_all_factors()
    all_fns = {r["name"]: r.get("compute_fn") for r in rows}
    # 补充 compute_fn/academic_source/direction/formula (get_all_factors 未含)
    conn = repo._conn()
    try:
        ext = conn.execute(
            "SELECT name, compute_fn, academic_source, direction, formula, updated_at "
            "FROM factor_registry").fetchall()
    finally:
        conn.close()
    ext_map = {r[0]: r for r in ext}

    items = []
    for r in rows:
        e = ext_map.get(r["name"], ())
        lineage = _derive_lineage(
            compute_fn=e[1] if len(e) > 1 else None,
            formula=e[4] if len(e) > 4 else None,
            all_fns=all_fns)
        up, down = lineage["upstream"], lineage["downstream"]
        items.append({
            "name": r["name"],
            "category": r["category"],
            "status": r["status"],
            "status_reason": r.get("status_reason"),
            "ic_mean": r.get("ic_mean"),
            "ic_ir": r.get("ic_ir"),
            "direction": e[3] if len(e) > 3 else None,
            "academic_source": e[2] if len(e) > 2 else None,
            "formula": e[4] if len(e) > 4 else None,
            "compute_fn": e[1] if len(e) > 1 else None,
            "updated_at": e[5] if len(e) > 5 else None,
            "lineage": {"upstream": up, "downstream": down},
        })

    from quant.factor.state_machine import FactorStateMachine
    _fsm = FactorStateMachine()
    state = {
        "active": _fsm.get_active_factors(),
        "probation": _fsm.get_probation_factors(),
        "evaluating": [f["name"] for f in repo.get_all_by_status(("evaluating",))],
        "archived": [f["name"] for f in repo.get_all_by_status(("archived",))],
    }
    return {"factors": items, "state": state,
            "counts": {k: len(v) for k, v in state.items()}}


def factor_lineage(name: str, version: Optional[str] = None) -> dict:
    """单个因子的血缘: 从 factor_registry (market.db) 实时推导."""
    snap = factor_platform_snapshot()
    target = next((f for f in snap["factors"] if f["name"] == name), None)
    if target is None:
        raise KeyError(f"factor {name} not found")
    return {
        "name": name,
        "metadata": {
            "category": target["category"], "status": target["status"],
            "ic_mean": target["ic_mean"], "ic_ir": target["ic_ir"],
            "direction": target["direction"],
            "academic_source": target["academic_source"],
            "formula": target["formula"], "compute_fn": target["compute_fn"],
        },
        "lineage": target["lineage"],
    }


# ═══════════════════════════════════════════════════════════
# 多策略 (quant.strategy)
# ═══════════════════════════════════════════════════════════

def strategy_summary() -> dict:
    """多策略全局总览 — 真实账户数据 (v506 fix: 原读空内存 StrategyManager → 全 0).

    数据源: 唯一真相源 trades.db (strategy_config + sim_trades) + 最新收盘价估值,
    经 PositionService.get_portfolio_summary 聚合. StrategyManager 纯内存空壳
    (无人 register) 不作为数据源.
    """
    from quant.core.state_broker import broker
    from web.services import PositionService

    strats = {}
    names = []
    # 真实在用的策略名: strategy_config 表里 initialized 的策略; broker positions
    # 的 strategy 字段为兜底 (运行时实际持仓归属)
    try:
        from quant.data.repos import TradeRepo
        repo = TradeRepo()
        conn = repo._conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT strategy FROM strategy_config WHERE COALESCE(initialized,0)=1"
            ).fetchall()
            names = [r[0] for r in rows] or ["quant"]
        finally:
            conn.close()
    except Exception:
        names = ["quant"]

    state = broker.get()
    pos_by_strat = {}
    for p in state.get("positions", []):
        pos_by_strat.setdefault(p.get("strategy", "quant"), []).append(p)
    names = sorted(set(names) | set(pos_by_strat.keys()))

    total_asset = total_cash = total_pnl = total_pos = 0.0
    for n in names:
        try:
            summ = PositionService.get_portfolio_summary(n)
            pos_list = pos_by_strat.get(n, [])
            total_asset += summ.get("total_asset", 0) or 0
            total_cash += summ.get("cash", 0) or 0
            total_pos += summ.get("position_value", 0) or 0
            total_pnl += summ.get("total_pnl", 0) or 0
            strats[n] = {
                "status": "active",
                "metrics": {
                    "position_value": summ.get("position_value", 0) or 0,
                    "available_cash": summ.get("cash", 0) or 0,
                    "total_pnl": summ.get("total_pnl", 0) or 0,
                    "positions": len(pos_list),
                },
            }
        except Exception as _e:
            logger.warning("strategy_summary[%s] unavailable: %s", n, _e)
            strats[n] = {"status": "unknown", "metrics": {}}

    return {
        "total_asset": round(total_asset, 2),
        "total_cash": round(total_cash, 2),
        "total_pnl": round(total_pnl, 2),
        "capital_utilization": (total_pos / max(total_asset, 1)),
        "active_strategies": len(strats),
        "strategies": strats,
        "source": "trades.db + 收盘价估值",
    }


def strategy_detail(name: str) -> dict:
    """单个策略详情 — 真实账户指标 + StrategyManager 状态交叉."""
    from web.services import PositionService
    summ = PositionService.get_portfolio_summary(name)
    from quant.strategy import get_strategy_manager
    mgr = get_strategy_manager()
    inst = mgr.get(name)
    status = getattr(inst, "status", "active") if inst is not None else "active"
    return {
        "name": name,
        "status": status,
        "metrics": {
            "portfolio_value": summ.get("total_asset", 0),
            "available_cash": summ.get("cash", 0),
            "total_pnl": summ.get("total_pnl", 0),
            "position_value": summ.get("position_value", 0),
            "initial_capital": summ.get("initial_capital", 0),
            "positions": len(PositionService.get_live_positions(name)),
        },
    }


def strategy_action(name: Optional[str], action: str) -> dict:
    """策略启停/调仓操作. action ∈ start/stop/pause/resume/rebalance."""
    from quant.strategy import get_strategy_manager
    mgr = get_strategy_manager()
    if action == "start":
        ok = mgr.start(name)
    elif action == "stop":
        ok = mgr.stop(name)
    elif action == "pause":
        ok = mgr.pause(name) if name else False
    elif action == "resume":
        ok = mgr.resume(name) if name else False
    elif action == "rebalance":
        if name:
            ok = mgr.rebalance_all()  # rebalance_all 接受 date 参数, name 语义为全部
        else:
            ok = mgr.rebalance_all()
    else:
        raise ValueError(f"unknown action {action}")
    return {"ok": bool(ok), "action": action, "name": name}


# ═══════════════════════════════════════════════════════════
# 另类数据 (quant.data.alternative)
# ═══════════════════════════════════════════════════════════

def alternative_sources() -> dict:
    """另类数据源列表 + 启用状态."""
    from quant.data.alternative import get_alternative_manager
    mgr = get_alternative_manager()
    sources = []
    for cfg in mgr.list_sources():
        sources.append({
            "name": cfg.name,
            "source_type": cfg.source_type.value if hasattr(cfg.source_type, "value") else cfg.source_type,
            "frequency": cfg.frequency.value if hasattr(cfg.frequency, "value") else cfg.frequency,
            "enabled": bool(cfg.enabled),
            "priority": cfg.priority,
            "symbols": (cfg.symbols or []),
            "start_date": cfg.start_date,
            "end_date": cfg.end_date,
        })
    # 已生成的另类因子表行数 (alternative_factors)
    n_rows = 0
    try:
        import sqlite3
        from quant.config.paths import MARKET_DB
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA busy_timeout=3000")
        try:
            n_rows = conn.execute(
                "SELECT COUNT(*) FROM alternative_factors").fetchone()[0]
        except Exception:
            pass
        conn.close()
    except Exception:
        pass
    return {"sources": sources, "factor_rows": n_rows}


# ═══════════════════════════════════════════════════════════
# 分布式回测 (quant.backtest.distributed)
# ═══════════════════════════════════════════════════════════

_dist_worker = None      # {run_id, task}
_dist_lock = threading.Lock()


class _GridWorker(threading.Thread):
    """后台线程跑分布式网格搜索, 结果逐条落 backtest_runs 表."""

    def __init__(self, run_id: str, param_grid: dict, fixed_params: dict,
                 backend: str, n_workers: int):
        super().__init__(daemon=True, name=f"dist-grid-{run_id[:8]}")
        self.run_id = run_id
        self.param_grid = param_grid
        self.fixed_params = fixed_params
        self.backend = backend
        self.n_workers = n_workers
        self.done = 0
        self.total = 1
        self.error = None

    def run(self):
        from quant.backtest.distributed import DistributedBacktestEngine
        engine = DistributedBacktestEngine(backend=self.backend,
                                           n_workers=self.n_workers)
        try:
            engine.start()
            # 注意: strategy 由 _run_single_backtest 用 params.run_id 落表,
            # 不塞进 param dict (BacktestParamSet 无 strategy 字段)
            params = self.fixed_params.copy()
            self.total = 1
            for k, vals in self.param_grid.items():
                if vals:
                    self.total *= len(vals)

            def _cb(_r):
                self.done += 1
                try:
                    engine.save_results([_r])
                except Exception as _e:
                    logger.warning("save_results failed for %s: %s", _r.run_id, _e)
            engine.run_grid_search(self.param_grid, params, _cb)
        except Exception as _e:
            self.error = str(_e)
            logger.error("dist grid %s failed: %s", self.run_id, _e)
        finally:
            try:
                engine.stop()
            except Exception:
                pass


def dist_submit(param_grid: dict, fixed_params: dict, backend: str = "thread",
                n_workers: int = 4) -> dict:
    """提交分布式网格搜索任务 (后台线程)."""
    import uuid
    from datetime import datetime, date
    # 提交前校验: 日期必须合法 (worker 崩溃会占 ber 单运行位, 宁可提交时拦)
    for key in ("start_date", "end_date"):
        val = fixed_params.get(key)
        if not val:
            raise ValueError(f"fixed_params 必须含 {key}")
        try:
            s_val = str(val)
            datetime.strptime(s_val, "%Y-%m-%d").date()
        except (ValueError, TypeError) as _e:
            raise ValueError(f"fixed_params.{key} 非法日期: {val}") from _e
    if str(fixed_params.get("start_date")) > str(fixed_params.get("end_date")):
        raise ValueError("start_date 不能晚于 end_date")
    run_id = f"grid_{uuid.uuid4().hex[:12]}"
    worker = _GridWorker(run_id, param_grid, fixed_params, backend, n_workers)
    with _dist_lock:
        global _dist_worker
        if _dist_worker and _dist_worker.is_alive():
            raise RuntimeError("已有网格任务运行中, 请等待完成或等待自动保存")
        _dist_worker = worker
    worker.start()
    return {"run_id": run_id, "total": worker.total}


def dist_status() -> dict:
    """当前网格任务状态 + 最近分布式结果."""
    import sqlite3
    from quant.config.paths import BACKTEST_DB
    with _dist_lock:
        w = _dist_worker
        running = bool(w and w.is_alive())
        if running:
            run_id = w.run_id
            run_total = w.total
            run_done = w.done
            run_error = w.error
        else:
            run_id = w.run_id if w else None
            run_total = w.total if w else 0
            run_done = w.done if w else 0
            run_error = w.error if w else None
    # 最近 10 条分布式结果 (_run_single_backtest 用 params.run_id = bt_* 落表)
    recent = []
    try:
        conn = sqlite3.connect(BACKTEST_DB)
        conn.execute("PRAGMA busy_timeout=3000")
        rows = conn.execute(
            "SELECT strategy, start_date, end_date, initial_capital, sharpe, "
            "cagr_pct, max_dd_pct, final_equity, elapsed_sec, started_at, errors "
            "FROM backtest_runs WHERE strategy LIKE 'bt_%' OR strategy LIKE 'grid_%' "
            "ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        recent = [{"run_id": r[0], "start": r[1], "end": r[2], "capital": r[3],
                   "sharpe": r[4], "cagr": r[5], "mdd": r[6], "equity": r[7],
                   "elapsed": r[8], "at": r[9], "errors": r[10]} for r in rows]
    except Exception:
        pass
    return {"running": running, "run_id": run_id, "total": run_total,
            "done": run_done, "error": run_error, "recent": recent}


# ═══════════════════════════════════════════════════════════
# 模型服务 (quant.alpha.model_serving) — 依赖 mlflow/bentoml 懒加载
# ═══════════════════════════════════════════════════════════

def model_serving_info() -> dict:
    """模型服务状态. mlflow/bentoml 未安装 → 返回依赖缺失标记 (不崩 web)."""
    result = {"available": False, "models": [], "stage_counts": {}, "reason": ""}
    try:
        from quant.alpha import model_serving as _ms
    except ImportError as _ie:
        result["reason"] = f"依赖未安装: {_ie}"
        return result
    try:
        platform = _ms.get_model_serving()
        # MLflow 模型列表 (尝试读取)
        models = []
        try:
            import mlflow
            client = mlflow.tracking.MlflowClient()
            for mv in client.search_registered_models():
                name = mv.name
                for v in (mv.latest_versions or []):
                    models.append({
                        "name": name, "version": v.version,
                        "stage": v.current_stage, "run_id": v.run_id,
                    })
        except Exception as _e:
            result["reason"] = f"MLflow 读取失败: {_e}"
        result["available"] = True
        result["models"] = models
        return result
    except Exception as _e:  # pragma: no cover
        result["reason"] = f"模型服务状态异常: {_e}"
        return result


# ═══════════════════════════════════════════════════════════
# Prometheus / Grafana
# ═══════════════════════════════════════════════════════════

def prometheus_metrics() -> bytes:
    """Prometheus 文本格式指标 (MonitoringPlatform.generate_latest)."""
    from quant.monitoring.prometheus import get_monitoring
    m = get_monitoring()
    return m.get_metrics()


def prometheus_status() -> dict:
    """Prometheus 指标摘要: 系列数 + 头发采样值 (系统页 KPI 用)."""
    text = prometheus_metrics().decode("utf-8", "replace")
    series = [l for l in text.splitlines() if l and not l.startswith("#")]
    names = [s.split()[0] for s in series]
    return {
        "count": len(series),
        "families": sorted(set(n.split("{", 1)[0] for n in names)),
        "samples": [s.split()[0] + "=" + s.split()[1] for s in series[:3]],
    }


def grafana_status() -> dict:
    """Grafana/Prometheus 端口探测 (仅状态, 不在 web 进程内起任何服务)."""
    import socket

    def _probe(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    p3000 = _probe(3000)
    return {"running": p3000, "url": "http://localhost:3000" if p3000 else None,
            "prometheus_running": _probe(9090),
            "prometheus_url": "http://localhost:9090",
            "hint": "Grafana 未运行 — 如需接入请先启动 (默认端口 3000)"}