"""独立自动分析脚本 — 由 launchd 定时调用"""
import sys, os
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from config.loader import get as cfg
TOKEN = os.environ.get("TUSHARE_TOKEN") or cfg("data.tushare_token") or ""
sys.path.insert(0, os.path.dirname(__file__))

from utils.logger import get_logger
logger = get_logger("auto_run")

from web.pipeline import RecommendationEngine
from web.db import init_db, save_result
from factor.cache import update_cache

def _get_prev_picks() -> set:
    """获取上次分析的推荐代码集合"""
    import sqlite3, json, os
    db = os.path.join(os.path.dirname(__file__), "data/results.db")
    if not os.path.exists(db): return set()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT raw_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if row:
        try:
            prev = json.loads(row[0])
            return set(r["symbol"] for r in prev.get("recommendations", []))
        except Exception:
            logger.warning("failed to parse previous picks")
            return set()


def _write_status(status: str, detail: str = "", alerts: list = None, last_sync: str = None):
    import json
    f = os.path.join(os.path.dirname(__file__), "data/auto_status.json")
    payload = {"last_run": datetime.now().isoformat(), "status": status, "detail": detail}
    if alerts:
        payload["alerts"] = alerts
    if last_sync:
        payload["last_sync"] = last_sync
    # 原子写入：先写临时文件再 rename 避免读取到半截数据
    tmp = f + ".tmp"
    with open(tmp, 'w') as fp:
        json.dump(payload, fp, ensure_ascii=False)
    os.replace(tmp, f)


if __name__ == "__main__":
    init_db()
    t0 = datetime.now()
    logger.info("auto analysis started")
    _write_status("running", "分析进行中...")

    try:
        engine = RecommendationEngine(tushare_token=TOKEN)

        # 确保数据就绪：股票列表 → 日线 → 基本面 → 因子缓存
        # 每个步骤独立 try/except，单步失败不阻塞后续步骤
        store = engine.store
        sync_errors = []
        if store.get_stock_count()["stocks"] < 100:
            logger.info("syncing stock list...")
            try:
                store.sync_stock_list()
            except Exception as e:
                sync_errors.append(f"stock_list: {e}")
                logger.exception("stock list sync failed")
        if store.get_stock_count()["daily_rows"] < 10000:
            logger.info("syncing daily data...")
            try:
                store.update_daily()
            except Exception as e:
                sync_errors.append(f"daily: {e}")
                logger.exception("daily sync failed")
        logger.info("syncing fundamentals...")
        try:
            store.sync_fundamentals()
        except Exception as e:
            sync_errors.append(f"fundamentals: {e}")
            logger.exception("fundamentals sync failed")
        logger.info("updating factor cache...")
        try:
            update_cache(engine.store)
        except Exception as e:
            sync_errors.append(f"factor_cache: {e}")
            logger.exception("factor cache update failed")
        if sync_errors:
            logger.warning(f"sync errors ({len(sync_errors)}): {'; '.join(sync_errors)}")

        prev_picks = _get_prev_picks()
        result = engine.run()
        if "error" in result:
            _write_status("failed", result["error"])
            logger.error(f"analysis failed: {result['error']}")
            sys.exit(1)
        save_result(result)

        # 追踪上一次推荐的表现
        try:
            from engine.tracker import init_tracking, track_previous_picks
            init_tracking()
            tracked = track_previous_picks(store=engine.store)
            if tracked and "error" not in tracked:
                logger.info(f"tracking: {tracked.get('n_picks', 0)} picks, "
                           f"hit_rate={tracked.get('hit_rate', 0)}%, avg={tracked.get('avg_return', 0)}%")
        except Exception:
            logger.exception("tracking failed")

        m = result["metrics"]
        elapsed = (datetime.now() - t0).total_seconds()
        new_picks = set(r["symbol"] for r in result["recommendations"])
        new_in = new_picks - prev_picks if prev_picks else set()
        dropped = prev_picks - new_picks if prev_picks else set()

        logger.info(f"done: {result['n_stocks']}只 夏普={m['sharpe_ratio']:.3f} 年化={m['annual_return']*100:.1f}% 耗时{elapsed:.0f}s")
        logger.info(f"  Top 5: {', '.join(r['symbol'] for r in result['recommendations'][:5])}")

        alerts = []
        if new_in:
            alert_new = f"新进: {', '.join(sorted(new_in))}"
            logger.warning(f"  *** {alert_new}")
            alerts.append({"type": "new", "msg": alert_new})
        if dropped:
            alert_drop = f"掉出: {', '.join(sorted(dropped))}"
            logger.warning(f"  *** {alert_drop}")
            alerts.append({"type": "drop", "msg": alert_drop})
        top_score = result['recommendations'][0]['score'] if result['recommendations'] else 0
        if top_score > 0.005:
            alert_score = f"高分信号! {result['recommendations'][0]['symbol']} {result['recommendations'][0].get('name','')} score={top_score:.4f}"
            logger.warning(f"  *** {alert_score}")
            alerts.append({"type": "high_score", "msg": alert_score})

        _write_status("success", f"完成 {result['n_stocks']}只 夏普{m['sharpe_ratio']:.3f} 耗时{elapsed:.0f}s", alerts)

    except Exception as e:
        import traceback
        elapsed = (datetime.now() - t0).total_seconds()
        err_msg = f"{type(e).__name__}: {e}"
        logger.error(f"failed ({elapsed:.0f}s): {err_msg}")
        logger.exception(f"analysis failed")
        _write_status("failed", f"{err_msg} (运行{elapsed:.0f}s后失败)")
