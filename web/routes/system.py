"""Web routes — system group."""
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from datetime import datetime, timedelta
from web.app import app, _require_token, _api_response, _require_cfg, cfg, logger, VERSION, get_current_date
from web.services import PositionService, BacktestService, StockService, SignalService
from quant.config.paths import TRADE_DB, MARKET_DB, BACKTEST_DB
from quant.config.loader import get as _cfg
from quant.data.store import market_conn
from web.admin_services import grafana_status as _grafana_status, model_serving_info as _model_info, prometheus_metrics as _prom_metrics, prometheus_status as _ps
from quant.core.state_broker import broker
from quant.execution.calendar import is_trading_day

@app.get("/openapi.json")
def api_openapi():
    """OpenAPI 3.0 规范 (模板 6)"""
    return {
        "openapi": "3.0.3",
        "info": {"title": "quant API", "version": "1.0.0", "description": "A股量化选股系统 API"},
        "paths": {
            "/api/state": {
                "get": {"summary": "系统状态", "responses": {"200": {"description": "OK"}}},
                "post": {"summary": "更新系统状态", "responses": {"200": {"description": "OK"}}}
            },
            "/api/factors": {"get": {"summary": "因子评估数据", "parameters": [{"name": "refresh", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/positions": {"get": {"summary": "当前持仓", "parameters": [{"name": "strategy", "in": "query"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}}, {"name": "offset", "in": "query", "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/trades": {"get": {"summary": "交易历史", "parameters": [{"name": "strategy", "in": "query"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}}, {"name": "offset", "in": "query", "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}},
            "/api/trade": {"post": {"summary": "记录交易", "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": {"description": "OK"}, "400": {"description": "参数错误"}}}},
            "/api/stream": {"get": {"summary": "SSE 状态推送 (实时)", "responses": {"200": {"description": "text/event-stream"}}}}, "/api/quotes": {"get": {"summary": "实时行情", "parameters": [{"name": "symbols", "in": "query", "required": True}], "responses": {"200": {"description": "OK"}}}},
            "/api/performance": {"get": {"summary": "绩效统计", "parameters": [{"name": "strategy", "in": "query"}, {"name": "quotes", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "OK"}}}},
        }
    }
def api_stream():
    """模板 6 + 方案B: SSE 实时推送状态变更 (替代轮询)."""
    import json, queue
    from fastapi.responses import Response
    q = broker.subscribe()
    from quant.execution.calendar import get_trading_period as _sp
    def generate():
        try:
            # 先发当前状态
            init = broker.get()
            init["status"] = _sp()
            yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = q.get(timeout=_require_cfg("web.sse.queue_timeout"))
                    data["status"] = _sp(); yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            broker.unsubscribe(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/recon")
def api_recon():
    """OMS 日终对账 (reconcile.py 落库). ?date=YYYY-MM-DD 缺省取最近对账日."""
    try:
        from quant.scheduler.reconcile import get_recon
        day = request.query_params.get("date") or None
        return _api_response(data=get_recon(day=day))
    except Exception as e:
        logger.warning(f"api_recon failed: {e}")
        return _api_response(
            error={"code": "INTERNAL", "message": "recon query failed"}, status_code=500)

@app.get("/api/health")
def api_health():
    """模板9 T1: 健康检查 — DB连接 + 最近 pipeline 状态."""
    import sqlite3, os as _os, time as _time
    status = {"status": "ok", "checks": {}}
    # DB 连通性 (C14: 每次请求后必须 close, 原实现泄漏连接)
    conn = None
    try:
        conn = market_conn("ro")
        conn.execute("SELECT 1").fetchone()
        status["checks"]["market_db"] = "ok"
    except Exception as e:
        status["checks"]["market_db"] = f"fail: {e}"
        status["status"] = "degraded"
    finally:
        if conn:
            conn.close()
    # 最近 pipeline 状态
    state = broker.get()
    status["pipeline"] = {
        "last_progress": state.get("progress", ""),
        "last_trace_id": state.get("trace_id", ""),
    }
    from quant.monitor.metrics import metrics as _mm
    status["metrics"] = _mm.snapshot()
    # 告警检查
    from quant.monitor.alerts import check_alerts
    status["alerts"] = check_alerts(state, _mm.snapshot())
    return _api_response(data=status)

@app.get("/api/scheduler")
def api_scheduler():
    """调度器状态 — DB 驱动的任务监控 (不再从日志刮取)."""
    import json, os
    from datetime import datetime
    import sqlite3
    from quant.scheduler.status import register_all, all_tasks
    from quant.execution.calendar import is_trading_day

    _proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cron_marker = os.path.join(_proj, ".cron_installed")
    today_str = get_current_date().isoformat()

    # ── 1. crontab 配置检测 ──
    # ── 从单一真相源 (status.register_all) 获取任务定义 ──
    register_all()
    cron_installed = os.path.exists(cron_marker)
    cron_tasks = {t["name"] for t in all_tasks()} if cron_installed else set()

    # ── 2. DB 查询 (统一入口: market.db → task_runs 表) ──
    # v562 fix: get MARKET_DB at runtime to avoid module-level caching
    from quant.config.paths import get_market_db_path
    MARKET_DB = get_market_db_path()
    # v556 (F7): 移除 v425 的 _check_timeouts 写库调用 — web 查询时标 aborted
    # 与 orchestrator 并发, 合法运行任务超 grace 被误标 → finish 跳过 →
    # 行恒 aborted + 冗余重跑 + 预算误耗. 超时自愈是 orchestrator 职责
    # (B22 每 30s 全日期检测), web 只读展示; 界面超时显示由下方
    # _API_TIMEOUTS 只读检测覆盖.
    db_runs = {}  # task_name → {status, finished_at, error, summary}
    db_runs_today = {}  # task_name → [今日所有记录列表]

    def _today_latest(key):
        """获取今日最新记录 (列表第一个是数据库中最新).
        """
        lst = db_runs_today.get(key)
        return lst[0] if lst else None

    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        recent = (datetime.combine(get_current_date(), datetime.min.time()) - timedelta(days=3)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT task_name, status, started_at, finished_at, error, summary, date "
            "FROM task_runs WHERE date >= ? ORDER BY date DESC, id DESC",
            (recent,)
        ).fetchall()
        for r in rows:
            key = r["task_name"]
            # v621 fix: 用 started_at 判断"今日执行"，而非 date 字段
            # date 是任务处理的日期（如 execute 处理 2026-09-04 的数据），
            # started_at 是任务实际执行的日期（2026-09-07）
            _started_date = r["started_at"][:10] if r["started_at"] else None
            _is_today = _started_date == today_str

            if key not in db_runs:  # 第一条是全局最新
                db_runs[key] = {
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "error": r["error"],
                    "summary": r["summary"],
                    "date": r["date"],
                }
            if _is_today:  # 今日所有记录（用 started_at 判断）
                db_runs_today.setdefault(key, []).append({
                    "task_name": r["task_name"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "error": r["error"],
                    "summary": r["summary"],
                    "date": r["date"],
                })
        conn.close()
    except Exception:
        pass  # 表可能还不存在，跳过

    # ── 3. 任务清单 ──
    _raw = all_tasks()
    tasks = []
    for rt in _raw:
        t = dict(rt)
        t["task"] = t.pop("label")
        t["key"] = t.pop("name")
        tasks.append(t)

    def _badge(cls, text):
        colors = {
            "green":  "#16a34a", "yellow": "#b45309",
            "blue":   "#2563eb",
            "red":    "#dc2626", "gray":   "#6b7280",
        }
        return f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600;background:{colors[cls]}18;color:{colors[cls]}">{text}</span>'

    for t in tasks:
        key = t["key"]
        has_cron = key in cron_tasks
        run = db_runs.get(key)
        run_today = _today_latest(key)

        # v565: 从 schedule 字段提取 HH:MM, 判断是否已过 cron 启动时间
        # v569: 修复 day-of-week 判断 — 仅当今天匹配 schedule 中的星期限制时才视为 past_cron
        import re as _re
        cron_hhmm = None
        _scheduled_today = True  # 默认当天就该运行 (无星期限制)
        _is_dependency_task = False  # v594: 标记是否为依赖任务 (如 daily_data完成后)
        schedule_str = t.get("schedule", "")
        if has_cron:
            # 提取时间 HH:MM
            m = _re.search(r'(\d{2}):(\d{2})', schedule_str)
            if m:
                try:
                    cron_hhmm = int(m.group(1)) * 60 + int(m.group(2))
                except (ValueError, TypeError):
                    cron_hhmm = None
            else:
                # v594: 没有 HH:MM 的任务是依赖任务 (如 daily_data完成后)
                _is_dependency_task = True
            # 检查星期限制: "周六 06:00", "周一/周四 factor_cache完成后" 等
            _today_wday = datetime.now().weekday()  # 0=Mon, 6=Sun
            _chinese_wday = "周" + ["一","二","三","四","五","六","日"][_today_wday]
            _schedule_wdays = set()
            # v616 fix: 支持 "周一/周四" 格式 (原正则只能匹配单个字符)
            for wm in _re.finditer(r'周[一二三四五六日](?:/[一二三四五六日])*', schedule_str):
                _wds = wm.group(0)[1:].split('/')  # 去掉"周"前缀，分割"/"
                _schedule_wdays.update(_wds)
            if _schedule_wdays:
                _scheduled_today = _chinese_wday[1:] in _schedule_wdays
        now_dt = datetime.now()
        now_hhmm = now_dt.hour * 60 + now_dt.minute
        # 仅当 (今天在 cron 的星期范围内) 或 (任务无星期限制) 且时间已过时, 才判定 past_cron
        past_cron = cron_hhmm is not None and _scheduled_today and now_hhmm >= cron_hhmm

        _is_trading = is_trading_day(get_current_date())

        # v568: 非交易日 → 所有有 cron 的任务统一显示"今日跳过", 避免误判"过时"/"未配置"
        # v592: 但如果有最近的执行记录 (db_runs 有全局最新记录), 显示该记录的状态+最后运行时间
        # 而不是完全跳过 — 这样周末界面仍能看到上一个交易日的执行结果
        if has_cron and not _is_trading and not run_today and run is None:
            t["status_label"] = _badge("gray", "今日跳过")
            t["status"] = "skipped"
            t["last_run"] = "—"
            t["cron"] = "已配置" if has_cron else "未配置"
            continue

                # v594: 依赖任务处理 — attribution/lgb_train/xgb_train 等是
        # evening_chain 子阶段，不由 orchestrator 独立触发。
        # 不应显示"等待执行"，而应反映 evening_chain 的状态。
        _EVENING_CHAIN_STAGES = {"daily_data", "adj_factor", "duckdb_sync",
                                  "factor_cache", "attribution",
                                  "lgb_train", "xgb_train"}
        if _is_dependency_task and not run_today:
            if key in _EVENING_CHAIN_STAGES:
                # 晚间链子阶段 — 显示 evening_chain 状态
                _ec_run = _today_latest("evening_chain")
                if _ec_run:
                    _ec_status = _ec_run.get("status", "")
                    if _ec_status == "running":
                        t["status_label"] = _badge("blue", "晚间链中")
                        t["status"] = "running"
                    elif _ec_status == "ok":
                        t["status_label"] = _badge("gray", "链跳过")
                        t["status"] = "skipped"
                    elif _ec_status == "failed":
                        t["status_label"] = _badge("red", "链失败")
                        t["status"] = "error"
                        t["error_msg"] = (_ec_run.get("error") or "晚间链失败")[:120]
                    else:
                        t["status_label"] = _badge("gray", _ec_status)
                        t["status"] = _ec_status
                else:
                    t["status_label"] = _badge("gray", "等待晚间链")
                    t["status"] = "pending"
                t["last_run"] = "—"
                t["cron"] = "已配置"
                continue
            # v616 fix: 如果今天不是计划日，显示"今日跳过"而非"等待执行"
            if _schedule_wdays and not _scheduled_today:
                t["status_label"] = _badge("gray", "今日跳过")
                t["status"] = "skipped"
                t["last_run"] = "—"
                t["cron"] = "已配置"
                continue
            # 从 schedule 字段提取上游任务名 (如 "daily_data完成后" → "daily_data")
            _upstream_key = None
            for _upstream_candidate in ["daily_data", "adj_factor", "duckdb_sync", "factor_cache", "attribution"]:
                if _upstream_candidate in schedule_str:
                    _upstream_key = _upstream_candidate
                    break
            if _upstream_key:
                _upstream_run = _today_latest(_upstream_key)
                if _upstream_run and _upstream_run.get("status") in ("ok", "partial"):
                    # 上游已完成，但本任务未运行 → 显示等待执行
                    t["status_label"] = _badge("yellow", "等待执行")
                    t["status"] = "pending"
                    t["last_run"] = "—"
                    t["cron"] = "已配置"
                    continue
                elif _upstream_run and _upstream_run.get("status") == "running":
                    # 上游运行中
                    t["status_label"] = _badge("gray", "等待上游")
                    t["status"] = "pending"
                    t["last_run"] = "—"
                    t["cron"] = "已配置"
                    continue

        if run_today and (run_today.get("status", "") or "").strip().lower() == "running" and run_today.get("finished_at") is None:
            # v566: 检查是否是 stale running 记录（跨天后未完成）
            # 跨天运行的 running 记录如果超时（> 24h），视为 stale → 显示失败
            try:
                _STALE_TIMEOUTS = {"daily_repair": 24*3600, "signals": 24*3600,
                                  "execute": 24*3600, "snapshot_open": 24*3600,
                                  "snapshot_close": 24*3600, "reconcile": 24*3600,
                                  "daily_data": 24*3600, "adj_factor": 24*3600,
                                  "duckdb_sync": 24*3600, "factor_cache": 24*3600,
                                  "attribution": 24*3600, "lgb_train": 24*3600,
                                  "xgb_train": 24*3600, "weekly_eval": 72*3600}
                started = datetime.fromisoformat(run_today["started_at"])
                elapsed = (datetime.now() - started).total_seconds()
                limit = _STALE_TIMEOUTS.get(key, 24*3600)
                if elapsed > limit:
                    t["status_label"] = _badge("red", "运行超时")
                    t["status"] = "timeout"
                    t["last_run"] = (run_today["started_at"] or "")[:16].replace("T", " ")
                else:
                    # vXXX: 若今日已有 completed (partial/ok) 记录，优先显示 completed，
                    # 避免重复触发/僵尸 running 行覆盖实际完成状态。
                    _completed_today = any(
                        r.get("status") in ("ok", "partial")
                        for records in db_runs_today.values()
                        for r in records
                        if r.get("task_name") == key and r.get("date") == today_str
                    )
                    if _completed_today:
                        _comp = next(r for records in db_runs_today.values()
                                     for r in records
                                     if r.get("task_name") == key and r.get("status") in ("ok", "partial") and r.get("date") == today_str)
                        if _comp.get("status") == "ok":
                            t["status_label"] = _badge("green", "今日已执行")
                            t["status"] = "success"
                        else:
                            t["status_label"] = _badge("yellow", "今日部分完成")
                            t["status"] = "partial"
                        t["last_run"] = (_comp.get("finished_at") or _comp.get("started_at") or "")[:16].replace("T", " ")
                    else:
                        t["status_label"] = _badge("blue", "运行中")
                        t["status"] = "running"
                        t["last_run"] = (run_today["started_at"] or "")[:16].replace("T", " ")
            except Exception:
                t["status_label"] = _badge("blue", "运行中")
                t["status"] = "running"
                t["last_run"] = (run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "lunch":
            t["status_label"] = _badge("yellow", "午休中")
            t["status"] = "lunch"
            t["last_run"] = (run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "ok":
            t["status_label"] = _badge("green", "今日已执行")
            t["status"] = "success"
            t["last_run"] = (run_today["finished_at"] or run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "partial":
            # v594: partial 状态 — 主流程成功但部分子表失败 (如 aux 表审计失败)
            t["status_label"] = _badge("yellow", "今日部分完成")
            t["status"] = "partial"
            t["last_run"] = (run_today["finished_at"] or run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "failed":
            err = run_today["error"] or "未知错误"
            t["status_label"] = _badge("red", "今日失败")
            t["status"] = "error"
            t["error_msg"] = err[:120]
            t["last_run"] = (run_today["finished_at"] or run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "aborted":
            # v616 fix: 如果今天不在计划日内，显示"今日跳过"而非"异常终止"
            # （aborted 可能是旧的 zombie 记录被清理后的残留）
            if _schedule_wdays and not _scheduled_today:
                t["status_label"] = _badge("gray", "今日跳过")
                t["status"] = "skipped"
                t["last_run"] = "—"
            else:
                err = run_today["error"] or "任务异常终止"
                t["status_label"] = _badge("red", "异常终止")
                t["status"] = "aborted"
                t["error_msg"] = err[:120]
                t["last_run"] = (run_today["finished_at"] or run_today["started_at"] or "")[:16].replace("T", " ")
        elif run_today and (run_today.get("status", "") or "").strip().lower() == "skipped":
            # C10 (CODE-REVIEW): lgb_train 无 lightgbm 时落 skipped,
            # 修复前落入 else → 显示"未配置"(误导). 展示为独立的灰色徽标.
            t["status_label"] = _badge("gray", "今日跳过")
            t["status"] = "skipped"
            t["last_run"] = (run_today["finished_at"] or run_today["started_at"] or "")[:16].replace("T", " ")
        elif has_cron and not run_today and past_cron and _is_trading:
            # 已过启动时间但无 DB 记录 → 任务未执行 (过时)
            schedule = t.get("schedule", "")
            if key == "monitor" and "-" in schedule:
                # monitor 特殊处理：检查时间窗口
                import re as _re3
                in_window = False
                _window_end_min = None
                for m in _re3.finditer(r'(\d{2}):(\d{2})-(\d{2}):(\d{2})', schedule):
                    start_h, start_m, end_h, end_m = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                    start_min = start_h * 60 + start_m
                    end_min = end_h * 60 + end_m
                    if start_min <= now_hhmm <= end_min:
                        in_window = True
                        break
                    _window_end_min = end_min
                if in_window:
                    # 仍在运行窗口内, 视为运行中
                    t["status_label"] = _badge("blue", "运行中")
                    t["status"] = "running"
                    t["last_run"] = "—"
                elif _window_end_min and now_hhmm > _window_end_min:
                    # 窗口已结束, 检查今天是否运行过
                    # v594: monitor 是窗口任务, 窗口结束后不应补跑, 应显示实际状态
                    _monitor_history = [r for records in db_runs_today.values() for r in records if r.get("task_name") == "monitor"]
                    if _monitor_history:
                        # 今天运行过, 显示窗口已结束
                        t["status_label"] = _badge("green", "窗口已结束")
                        t["status"] = "success"
                        t["last_run"] = (_monitor_history[0].get("finished_at") or _monitor_history[0].get("started_at") or "")[:16].replace("T", " ")
                    else:
                        # 今天无 task_runs 记录 (Dagster 模式下 monitor daemon 可能因
                        # multiprocess executor 退出而未能写入记录), 但窗口内已乐观
                        # 显示"运行中", 窗口外应保持一致 → 显示"窗口已结束"
                        _has_other_today = any(
                            r.get("date") == today_str
                            for records in db_runs_today.values()
                            for r in records
                        )
                        if _has_other_today:
                            t["status_label"] = _badge("green", "窗口已结束")
                            t["status"] = "success"
                            t["last_run"] = "—"
                        else:
                            # 今天未运行, 显示窗口未运行 (不是过时未执行)
                            t["status_label"] = _badge("gray", "窗口未运行")
                            t["status"] = "skipped"
                            t["last_run"] = "—"
                else:
                    # 未到第一个窗口 → 等待调度
                    t["status_label"] = _badge("gray", "等待调度")
                    t["status"] = "pending"
                    t["last_run"] = "—"
            else:
                # v594: 兜底逻辑 - 尝试自动补跑（inline 任务）
                _auto_triggered = False
                try:
                    from quant.scheduler.manifest import spec as _spec
                    from quant.scheduler.runners import InlineRunner, _should_run
                    from datetime import time as _time_class  # v611: 避免 datetime.time 冲突
                    _task_spec = _spec(key)

                    # v611 fix: 在自动补跑前检查依赖是否就绪
                    # 如果 depends_attempt 指定的上游任务今日未尝试 (None/running),
                    # _should_run 返回 False → 不应强制补跑, 应显示依赖等待
                    if _task_spec and _task_spec.mode == "inline":
                        # 构建 status_dict: task_name → 最新 status 字符串
                        # (与 _get_today_status 保持一致的格式)
                        _hhmm = _time_class(now_dt.hour, now_dt.minute)
                        _weekday = now_dt.weekday()
                        _status_dict = {}
                        _aborted_dict = {}
                        for k, records in db_runs_today.items():
                            _latest = records[0]  # 第一条是最新的 (ORDER BY id DESC)
                            _status_dict[k] = _latest.get("status", "")
                            # 计算重试次数 (failed + aborted 计数, 供 _should_run 判断)
                            _aborted_dict[k] = sum(1 for r in records if r.get("status", "").lower() in ("failed", "aborted"))
                        _can_run = _should_run(_task_spec, _hhmm, _weekday, _status_dict, _aborted_dict)

                        if _can_run:
                            _runner = InlineRunner(today_str)
                            import threading as _thr
                            _t = _thr.Thread(target=_runner._dispatch, args=(_task_spec, None), daemon=True)
                            _t.start()
                            _auto_triggered = True
                        else:
                            # v612: 检查依赖是否满足
                            # v612 fix: monitor 模式守护进程不写 task_runs, 需要特殊处理
                            _deps_ok = _task_spec.depends_ok
                            _deps_attempt = _task_spec.depends_attempt
                            _deps_satisfied = True
                            _unmet = []
                            for dep in _deps_ok:
                                dep_st = _status_dict.get(dep)
                                if dep_st != "ok":
                                    _deps_satisfied = False
                                    _unmet.append(f"{dep}≠ok")
                            for dep in _deps_attempt:
                                dep_st = _status_dict.get(dep)
                                if dep_st is None or dep_st == "running":
                                    # v612: monitor 模式守护进程不写 task_runs, 需要特殊处理
                                    _dep_spec = _spec(dep) if dep else None
                                    if _dep_spec and _dep_spec.mode == "monitor":
                                        _win_start = _dep_spec.window[0] if _dep_spec.window else None
                                        _win_end = _dep_spec.window[1] if _dep_spec.window else None
                                        if _win_end and _hhmm >= _win_end:
                                            pass  # monitor 窗口已结束, 今天运行过了
                                        elif _win_start and _hhmm < _win_start:
                                            _deps_satisfied = False
                                            _unmet.append(f"{dep}未到窗口")
                                        else:
                                            pass  # 在窗口内, monitor 应该正在运行
                                    else:
                                        _deps_satisfied = False
                                        _unmet.append(f"{dep}未尝试")
                            if _deps_satisfied:
                                # v612: 依赖已满足, 让代码落到 else 分支显示昨日状态
                                pass
                            else:
                                _reason = ", ".join(_unmet)
                                t["status_label"] = _badge("gray", f"等待上游: {_reason}")
                                t["status"] = "pending"
                                t["last_run"] = "—"
                                t["error_msg"] = _reason
                                continue
                except Exception:
                    pass
                if _auto_triggered:
                    t["status_label"] = _badge("blue", "自动补跑中")
                    t["status"] = "running"
                    t["last_run"] = "—"
                else:
                    # v594: 检查是否昨天运行过, 如果是则显示实际状态
                    # 使用 started_at/finished_at 判断实际运行时间, 而非 date 字段
                    from datetime import date as _date
                    _today_str = _date.today().isoformat()
                    if run:
                        _task_date = (run.get("finished_at") or run.get("started_at") or "")[:10]
                        _is_today = _task_date == _today_str
                    else:
                        _is_today = False
                    if run and run["status"] == "ok":
                        if _is_today:
                            t["status_label"] = _badge("green", "今日已执行")
                            t["status"] = "success"
                        else:
                            t["status_label"] = _badge("green", "上日已执行")
                            t["status"] = "success"
                        t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                    elif run and run["status"] == "partial":
                        if _is_today:
                            t["status_label"] = _badge("yellow", "今日部分完成")
                            t["status"] = "partial"
                        else:
                            t["status_label"] = _badge("yellow", "上日部分完成")
                            t["status"] = "partial"
                        t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                    elif run and run["status"] == "failed":
                        if _is_today:
                            t["status_label"] = _badge("red", "今日失败")
                            t["status"] = "error"
                        else:
                            t["status_label"] = _badge("red", "上日失败")
                            t["status"] = "error"
                        t["error_msg"] = (run.get("error") or "未知错误")[:120]
                        t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                    elif run and run["status"] == "aborted":
                        if _is_today:
                            t["status_label"] = _badge("red", "今日异常终止")
                            t["status"] = "aborted"
                        else:
                            t["status_label"] = _badge("red", "上日异常终止")
                            t["status"] = "aborted"
                        t["error_msg"] = (run.get("error") or "未知错误")[:120]
                        t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                    else:
                        t["status_label"] = _badge("red", "过时未执行")
                        t["status"] = "missed"
                        t["last_run"] = "—"
        elif has_cron and not run_today:
            # v592: 非交易日 + 无今日记录 → 显示上一次执行状态而非"过时"/"等待"
            if not _is_trading:
                if run and run["status"] == "ok":
                    t["status_label"] = _badge("green", "上日已执行")
                    t["status"] = "success"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] in ("partial",):
                    t["status_label"] = _badge("gray", "上日部分完成")
                    t["status"] = "partial"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] == "failed":
                    t["status_label"] = _badge("red", "上日失败")
                    t["status"] = "error"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] == "aborted":
                    t["status_label"] = _badge("red", "上日异常终止")
                    t["status"] = "aborted"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                else:
                    t["status_label"] = _badge("gray", "等待下次交易日")
                    t["status"] = "pending"
                    t["last_run"] = "—"
            elif not past_cron:
                t["status_label"] = _badge("gray", "等待调度")
                t["status"] = "pending"
                t["last_run"] = "—"
            else:
                # 交易日, 已过 cron 时间, 但无记录 → 过时未执行
                # v594: 检查是否昨天运行过, 如果是则显示实际状态
                if run and run["status"] == "ok":
                    t["status_label"] = _badge("green", "上日已执行")
                    t["status"] = "success"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] == "partial":
                    t["status_label"] = _badge("yellow", "上日部分完成")
                    t["status"] = "partial"
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] == "failed":
                    t["status_label"] = _badge("red", "上日失败")
                    t["status"] = "error"
                    t["error_msg"] = (run.get("error") or "未知错误")[:120]
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                elif run and run["status"] == "aborted":
                    t["status_label"] = _badge("red", "上日异常终止")
                    t["status"] = "aborted"
                    t["error_msg"] = (run.get("error") or "未知错误")[:120]
                    t["last_run"] = (run.get("finished_at") or run.get("started_at") or "")[:16].replace("T", " ")
                else:
                    t["status_label"] = _badge("red", "过时未执行")
                    t["status"] = "missed"
                    t["last_run"] = "—"
        else:
            t["status_label"] = _badge("gray", "未配置")
            t["status"] = "unconfigured"
            t["last_run"] = "—"

        t["cron"] = "已配置" if has_cron else "未配置"

    # v565: 报告编排器模式 — Dagster 或 legacy
    from quant.scheduler import get_orchestrator_mode
    mode = get_orchestrator_mode()
    dagster_enabled = mode == "dagster"

    return _api_response(data={
        "tasks": tasks,
        "orchestrator_mode": mode,
        "dagster_enabled": dagster_enabled,
    })
@app.get("/api/metrics")
def api_metrics(request: Request):
    """模板9 T1: 指标快照 (Prometheus 本地等价)."""
    from quant.monitor.metrics import metrics as _mm
    return _api_response(data=_mm.snapshot())


@app.get("/api/benchmark")
def api_benchmark():
    """v409: 基准跟踪 — 累计曲线 + 滚动 alpha/IR/beta/capture.

    v536: 原裸 SQL 直查 benchmark_tracking 60 行 — 改为 benchmark.tracker
    get_tracking_summary() (累计曲线/滚动指标完整实现, 原零消费方).
    """
    from quant.benchmark.tracker import get_tracking_summary
    summary = get_tracking_summary(strategy="quant")
    if not summary.get("available"):
        return _api_response(data=summary)
    latest = summary.pop("latest_rolling", None)
    return _api_response(data={"summary": summary, "latest_rolling": latest})


# ═══════════════════════════════════════════════════════════
# 管理后台 API (v505 refactor: 因子平台已并入 /api/factors) — 多策略 / 另类 /
# 分布式回测 / 模型服务 / 监控
# ═══════════════════════════════════════════════════════════
from web.admin_services import (
    alternative_sources as _alt_sources,
    dist_status as _dist_status,
    dist_submit as _dist_submit,
    factor_lineage as _factor_lineage,
    grafana_status as _grafana_status,
    model_serving_info as _model_info,
    prometheus_metrics as _prom_metrics,
    strategy_action as _strategy_action,
    strategy_detail as _strategy_detail,
    strategy_summary as _strategy_summary,
)

@app.get("/api/factors/lineage")
def api_factor_lineage():
    """单因子血缘 (v505: 统一入 /api/factors 族)."""
    name = request.query_params.get("name", "")
    if not name:
        return _api_response(error={"code": "INVALID_PARAMETER",
                                    "message": "name required"}, status_code=400)
    try:
        return _api_response(data=_factor_lineage(name))
    except KeyError as e:
        return _api_response(error={"code": "NOT_FOUND", "message": str(e)},status_code=404)
    except Exception as e:
        logger.warning(f"factor lineage failed: {e}")
        return _api_response(error={"code": "INTERNAL",
                                    "message": "因子血缘查询失败"}, status_code=500)


@app.get("/api/strategy/summary")
def api_strategy_summary():
    return _api_response(data=_strategy_summary())


@app.get("/api/strategy/{name}")
def api_strategy_detail(name):
    try:
        return _api_response(data=_strategy_detail(name))
    except KeyError as e:
        return _api_response(error={"code": "NOT_FOUND", "message": str(e)},status_code=404)
    except Exception as e:
        return _api_response(error={"code": "INTERNAL",
                                    "message": "策略详情查询失败"}, status_code=500)


@app.post("/api/strategy/{name}/action")
async def api_strategy_action(name: str, request: Request):
    """策略启停/调仓. body: {"action": start|stop|pause|resume|rebalance}"""
    _auth = _require_token(request)
    if _auth:
        return _auth
    payload = await request.json()
    action = payload.get("action", "")
    try:
        return _api_response(data=_strategy_action(name, action))
    except ValueError as e:
        return _api_response(error={"code": "INVALID_PARAMETER", "message": str(e)},status_code=400)
    except Exception as e:
        return _api_response(error={"code": "INTERNAL", "message": str(e)},status_code=500)


@app.get("/api/alternative/sources")
def api_alternative_sources():
    try:
        return _api_response(data=_alt_sources())
    except Exception as e:
        return _api_response(error={"code": "INTERNAL",
                                    "message": "另类数据查询失败"}, status_code=500)


@app.post("/api/backtest/dist/submit")
async def api_backtest_dist_submit(request: Request):
    """提交分布式网格回测. body: {param_grid, fixed_params, backend, n_workers}"""
    _auth = _require_token(request)
    if _auth:
        return _auth
    payload = await request.json()
    param_grid = payload.get("param_grid") or {}
    fixed = payload.get("fixed_params") or {}
    backend = payload.get("backend", "thread")
    n_workers = int(payload.get("n_workers", 4))
    try:
        result = _dist_submit(param_grid, fixed, backend, n_workers)
        return _api_response(data=result)
    except RuntimeError as e:
        return _api_response(error={"code": "CONFLICT", "message": str(e)},status_code=409)
    except Exception as e:
        return _api_response(error={"code": "INTERNAL", "message": str(e)},status_code=500)


@app.get("/api/backtest/dist/status")
def api_backtest_dist_status():
    return _api_response(data=_dist_status())


@app.get("/api/model/serving")
def api_model_serving():
    return _api_response(data=_model_info())


@app.get("/api/monitoring/grafana")
def api_monitoring_grafana():
    return _api_response(data=_grafana_status())


@app.get("/api/monitoring/prometheus")
def api_monitoring_prometheus():
    from web.admin_services import prometheus_status as _prom_status
    return _api_response(data=_prom_status())


@app.get("/metrics")
def metrics_endpoint(request: Request):
    """Prometheus 文本格式指标 (探针/采集器消费)."""
    from fastapi.responses import Response
    try:
        return Response(_prom_metrics(), mimetype="text/plain; version=0.0.4")
    except Exception as e:
        logger.warning(f"/metrics failed: {e}")
        return _api_response(error={"code": "INTERNAL",
                                    "message": "Prometheus 指标生成失败"}, status_code=500)


@app.get("/api/monitoring/datasources")
def api_monitoring_datasources(request: Request):
    """Grafana 数据源摘要 — 供系统页判断 Prometheus 是否接入."""
    from quant.config.loader import get as _cfg
    return _api_response(data={
        "prometheus": {
            "url": f"http://localhost:{_cfg('prometheus.port')}",
            "configured": bool(_cfg("prometheus.enabled")),
        },
        "grafana": {
            "url": f"http://localhost:{_cfg('grafana.port')}",
            "configured": bool(_cfg("grafana.enabled")),
        },
    })


# ═══════════════════════════════════════════════════════════
# v536: 界面接入补充端点 — daily_risk 历史 / 评估历史 / phase8
# ═══════════════════════════════════════════════════════════

@app.get("/api/risk/history")
def api_risk_history():
    """每日 VaR/CVaR 历史 (daily_risk 表, v536 起晚间链写入)."""
    try:
        from quant.config.paths import TRADE_DB
        import sqlite3 as _sql
        conn = _sql.connect(TRADE_DB)
        conn.row_factory = _sql.Row
        # 晚间链 v536 起写 daily_risk; 首次部署前表可能不存在 → 返回空
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_risk ("
            "date TEXT PRIMARY KEY, var_95 REAL, var_95_pct REAL, cvar_95 REAL, "
            "cvar_95_pct REAL, portfolio_value REAL, n_positions INTEGER)"
        )
        rows = conn.execute(
            "SELECT date, var_95, var_95_pct, cvar_95, cvar_95_pct, "
            "portfolio_value, n_positions FROM daily_risk "
            "ORDER BY date DESC LIMIT 120"
        ).fetchall()
        conn.close()
        return _api_response(data=[dict(r) for r in rows])
    except Exception as e:
        logger.warning(f"api_risk_history failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": "daily_risk 查询失败"}, status_code=500)


@app.get("/api/evaluations")
def api_evaluations(request: Request):
    """评估历史 (evaluation_runs 表, 每周末 phase 链写入). ?phase=phase3"""
    from quant.evaluation.run_store import list_runs
    phase = request.query_params.get("phase")
    try:
        rows = list_runs(phase=phase, limit=15)
        return _api_response(data={"runs": rows, "phase": phase})
    except Exception as e:
        logger.warning(f"api_evaluations failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": "评估历史查询失败"}, status_code=500)


@app.get("/api/phase8")
def api_phase8():
    """phase8 回测 vs 实盘一致性报告. ?rerun=1 强制重算 (重, 默认读最近一次)."""
    try:
        if request.query_params.get("rerun") == "1":
            from quant.evaluation.phase8_live_consistency import validate_consistency
            result = validate_consistency()
        else:
            from quant.evaluation.phase8_live_consistency import get_latest_report
            result = get_latest_report() or {"status": "not_available",
                                             "message": "尚无 phase8 报告 — 点重跑生成"}
        return _api_response(data=result)
    except Exception as e:
        logger.warning(f"api_phase8 failed: {e}")
        return _api_response(error={"code": "INTERNAL", "message": f"phase8: {e}"}, status_code=500)


if __name__ == "__main__":
    port = int(_require_cfg("web.port"))
    from quant.monitoring.prometheus import init_monitoring
    init_monitoring()  # 启动 MetricsCollector (30s 系统指标 + 低频行数) — 模板 9
    logger.info(f"Web 服务启动于端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
