"""量化选股 Web 服务 — 后台分析, 永不阻塞"""
import json, os, threading, time
from flask import Flask, jsonify, render_template
from web.pipeline import RecommendationEngine
from web.db import init_db, save_result, get_history
from factor.compute import compute_factors
from data.store import DataStore
from utils.logger import get_logger

logger = get_logger("app")
from config.loader import get as cfg
TOKEN = os.environ.get("TUSHARE_TOKEN") or cfg("data.tushare_token") or ""

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

engine = None
_store = None
_analysis_running = False
_analysis_lock = threading.Lock()
_store_lock = threading.Lock()
_engine_lock = threading.Lock()


def get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DataStore(tushare_token=TOKEN)
    return _store


def get_engine():
    global engine
    if engine is None:
        with _engine_lock:
            if engine is None:
                engine = RecommendationEngine(tushare_token=TOKEN)
                init_db()
    return engine


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/latest")
def latest():
    """最新分析结果（读 SQLite, 永远不阻塞）"""
    history = get_history(limit=1)
    if history:
        r = history[0]
        return jsonify({
            "ok": True, "run_at": r["run_at"], "n_stocks": r["n_stocks"],
            "sharpe": r["sharpe"], "annual_return": r["annual_return"],
            "max_drawdown": r["max_drawdown"], "win_rate": r["win_rate"],
            "picks": r["picks"], "raw_json": r["raw_json"],
        })
    return jsonify({"ok": False, "msg": "暂无分析结果"})


@app.route("/api/run", methods=["POST"])
def run_analysis():
    """启动后台分析, 立即返回"""
    global _analysis_running, _analysis_lock
    with _analysis_lock:
        if _analysis_running:
            return jsonify({"ok": False, "msg": "分析进行中"})
        _analysis_running = True

    def _bg():
        global _analysis_running
        try:
            t0 = time.time()
            engine = get_engine()
            compute_factors(engine.store)
            result = engine.run()
            save_result(result)
            elapsed = time.time() - t0
            m = result.get("metrics", {})
            logger.info(f"analysis done: {elapsed:.0f}s sharpe={m.get('sharpe_ratio', 0):.3f}")
            # 同步更新 auto_status.json，让 /api/auto-status 展示最新 alert
            _sync_auto_status(result, elapsed)
            # 追踪上一次推荐的表现
            try:
                from engine.tracker import init_tracking, track_previous_picks
                init_tracking()
                track_previous_picks(store=engine.store)
            except Exception:
                logger.exception("tracking failed")
        except Exception:
            logger.exception("analysis failed")
        finally:
            with _analysis_lock:
                _analysis_running = False

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "msg": "分析已启动"})


@app.route("/api/auto-status")
def auto_status():
    f = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data/auto_status.json")
    if os.path.exists(f):
        with open(f) as fp:
            return jsonify(json.load(fp))
    return jsonify({"status": "unknown"})


def _sync_auto_status(result: dict, elapsed: float):
    """手动分析后同步 auto_status.json，确保 /api/auto-status 数据一致"""
    import json as j
    from datetime import datetime
    f = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data/auto_status.json")
    m = result.get("metrics", {})
    recs = result.get("recommendations", [])
    detail = f"完成 {result.get('n_stocks', '?')}只 夏普{m.get('sharpe_ratio', 0):.3f} 耗时{elapsed:.0f}s"
    alerts = []
    if recs:
        top = recs[0]
        alerts.append({
            "type": "high_score",
            "msg": f"高分信号! {top['symbol']} {top.get('name', '')} score={top['score']:.4f}"
        })
    payload = {"last_run": datetime.now().isoformat(), "status": "success", "detail": detail, "alerts": alerts}
    tmp = f + ".tmp"
    with open(tmp, 'w') as fp:
        j.dump(payload, fp, ensure_ascii=False)
    os.replace(tmp, f)


@app.route("/api/track")
def track():
    history = get_history(limit=1)
    if not history:
        return jsonify({"ok": False, "msg": "暂无记录"})
    r = history[0]
    import pandas as pd
    symbols = [p["symbol"] for p in r["picks"][:20]]
    prices = get_store().get_daily(symbols)
    close_batch = prices["close"].sort_index() if not prices.empty and "close" in prices else pd.DataFrame()
    tracked = []
    for p in r["picks"][:20]:
        sym, old = p["symbol"], p["price"]
        if sym in close_batch.columns and old > 0:
            new = float(close_batch[sym].iloc[-1])
            chg = (new / old - 1)
        else:
            new, chg = 0, 0
        tracked.append({
            "symbol": sym, "name": p["name"],
            "rec_price": old, "latest_price": round(new, 2),
            "change_pct": round(chg * 100, 2),
        })
    avg = sum(t["change_pct"] for t in tracked) / len(tracked) if tracked else 0
    win = sum(1 for t in tracked if t["change_pct"] > 0)
    return jsonify({
        "ok": True, "run_at": r["run_at"], "tracked": tracked,
        "avg_change": round(avg, 2), "win_rate": f"{win}/{len(tracked)}",
    })


@app.route("/api/kline/<symbol>")
def kline(symbol):
    """返回单只股票最近120天的 OHLCV 数据"""
    store = get_store()
    conn = store._connect()
    rows = conn.execute(
        "SELECT date,open,high,low,close,volume FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 120",
        (symbol,)
    ).fetchall()
    if not rows:
        return jsonify({"ok": False, "msg": "无数据"})
    rows.reverse()
    data = []
    for r in rows:
        data.append([
            r[0][:4]+"-"+r[0][4:6]+"-"+r[0][6:],  # YYYYMMDD → YYYY-MM-DD
            round(float(r[1] or 0), 2), round(float(r[4] or 0), 2),
            round(float(r[3] or 0), 2), round(float(r[2] or 0), 2),
            int(float(r[5] or 0)),
        ])
    return jsonify({"ok": True, "symbol": symbol, "data": data})


@app.route("/api/history")
def history():
    return jsonify(get_history(limit=10))


@app.route("/api/milestones")
def milestones():
    """北极星目标追踪: 5000→100万的阶段和里程碑"""
    try:
        from strategy.planner import get_stage, compute_milestones
        # 从最近回测结果估算日收益率
        history = get_history(limit=1)
        if history:
            m = history[0]
            ann_ret = m.get("annual_return", 0) or 0
            daily_ret = (1 + ann_ret) ** (1/252) - 1 if ann_ret > -1 else 0.005
        else:
            daily_ret = 0.005  # default: 0.5% daily (matches planner.py)

        capital = 5000  # start
        stage = get_stage(capital)
        milestones = compute_milestones(capital, max(daily_ret, 0.001))

        return jsonify({
            "ok": True,
            "current_stage": stage,
            "milestones": milestones,
            "estimated_daily_return": round(daily_ret * 100, 2),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/monitor")
def monitor_summary():
    """实盘偏差监控摘要"""
    try:
        from execution.monitor import get_monitor_summary, init_monitor_db
        init_monitor_db()
        return jsonify({"ok": True, **get_monitor_summary()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/strategy-config")
def strategy_config():
    """当前策略配置（根据资金规模自动调整）"""
    try:
        from strategy.planner import get_strategy_config
        config = get_strategy_config(5000)
        return jsonify({"ok": True, "config": config})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/tracking")
def tracking():
    """推荐追踪分析: 历史推荐的实际表现"""
    try:
        from engine.tracker import init_tracking, get_tracking_history, get_tracking_stats
        init_tracking()
        stats = get_tracking_stats()
        history = get_tracking_history(limit=10)
        return jsonify({"ok": True, "stats": stats, "history": history})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/tracking/latest")
def tracking_latest():
    """最近一次追踪的详细结果"""
    try:
        from engine.tracker import init_tracking, get_tracking_history
        init_tracking()
        history = get_tracking_history(limit=1)
        if history:
            return jsonify({"ok": True, "tracking": history[0]})
        return jsonify({"ok": False, "msg": "暂无追踪数据"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/positions")
def positions():
    """模拟持仓监控: 推荐即买入, 含最新估值和盈亏"""
    try:
        from engine.sim_broker import init_simulation, get_positions, get_portfolio_summary
        init_simulation()
        summary = get_portfolio_summary(store=get_store())
        pos = get_positions(store=get_store())
        return jsonify({"ok": True, "summary": summary, "positions": pos})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/trades")
def trades():
    """模拟交易记录"""
    try:
        from engine.sim_broker import init_simulation, get_trades
        init_simulation()
        return jsonify({"ok": True, "trades": get_trades(limit=50)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


def main():
    stats = get_engine().store.get_stock_count()
    logger.info(f"quant server started ({stats['stocks']} stocks/{stats['daily_rows']} rows)")
    app.run(host="0.0.0.0", port=8521, debug=False)


if __name__ == "__main__":
    main()
