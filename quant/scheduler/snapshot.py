"""开盘30分钟价格快照 (test-v324).

每日 09:30 执行, 快照所有A股的实时价, 供 intraday_reversal 因子使用.
60天积累后激活日内反转因子 (IC_IR≈0.8+, A股最强因子之一).

数据源: 腾讯财经实时行情 (qt.gtimg.cn), 批量拉取.
"""
import urllib.request, re, time as _time
from datetime import datetime
from quant.data.repos._base import DatabaseManager
from quant.utils.logger import get_logger
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish, task

_log = get_logger("snapshot.intraday")

_TENCENT_URL = "http://qt.gtimg.cn/q="
_BATCH_SIZE = 50
_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.qq.com",
}


def _symbol_to_tencent(s: str) -> str:
    if s.startswith("6"):
        return f"sh{s}"
    elif s.startswith(("0", "3")):
        return f"sz{s}"
    elif s.startswith("8") or s.startswith("4"):
        return f"bj{s}"
    return f"sz{s}"


def _fetch_batch(batch: list[str]) -> dict[str, dict]:
    """批量拉取实时价格+成交量. 返回 {symbol: {price, volume}}."""
    codes = ",".join(_symbol_to_tencent(s) for s in batch)
    req = urllib.request.Request(_TENCENT_URL + codes, headers=_TENCENT_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk")
    except Exception as e:
        _log.warning(f"snapshot fetch failed: {e}")
        return {}

    results = {}
    for line in text.strip().split("\n"):
        m = re.search(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        fields = m.group(2).split("~")
        if len(fields) < 7:
            continue
        try:
            price = float(fields[3]) if fields[3] else 0
            volume = int(float(fields[6])) if len(fields) > 6 and fields[6] else 0
            if price <= 0:
                continue
            symbol = m.group(1)[2:]
            results[symbol] = {"price": round(price, 2), "volume": volume}
        except (ValueError, IndexError):
            continue
    return results


def snapshot_open(today: str = None):
    """快照开盘30分钟价格+成交量."""
    return _snapshot_with_log(today, mode="open", task_name="snapshot_open")


@task("snapshot_open", grace_seconds=300)
def _snapshot_open_task(today: str):
    return _snapshot(today, mode="open")


def snapshot_close(today: str = None):
    """快照尾盘5分钟价格+成交量."""
    return _snapshot_with_log(today, mode="close", task_name="snapshot_close")


@task("snapshot_close", grace_seconds=300)
def _snapshot_close_task(today: str):
    return _snapshot(today, mode="close")


def _snapshot_with_log(today: str = None, mode: str = "open", task_name: str = "snapshot"):
    """快照 + task_runs 状态写入, 防止 orchestrator 重复触发."""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    t0 = _time.monotonic()
    _log.info(f"_snapshot_with_log start: task={task_name} date={today} mode={mode}")
    rid = _tk_start(task_name, today, grace_seconds=300)
    if rid is None:
        _log.info(f"[{today}] {task_name} already running, skip duplicate trigger")
        return
    try:
        result = _snapshot(today, mode)
        elapsed = _time.monotonic() - t0
        _log.info(f"_snapshot_with_log done: task={task_name} saved={result.get('saved',0)} errors={result.get('errors',0)} elapsed={elapsed:.1f}s")
        _tk_finish(task_name, today, "ok", summary=result)
        return result
    except Exception as e:
        elapsed = _time.monotonic() - t0
        _log.exception(f"_snapshot_with_log failed: task={task_name} elapsed={elapsed:.1f}s error={e}")
        _tk_finish(task_name, today, "failed", error=str(e))
        raise


def _snapshot(today: str = None, mode: str = "open"):
    """快照所有A股实时价到 intraday_snapshot 表."""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    from quant.data.repos.universe_repo import UniverseRepo
    from quant.data.repos._base import DatabaseManager

    symbols = UniverseRepo().get_symbols(exclude_market="BJ")
    label = "开盘" if mode == "open" else "尾盘"
    _log.info(f"snapshot {label}: {today} — {len(symbols)} stocks")

    conn = DatabaseManager.market()
    saved = 0
    errors = 0

    for i in range(0, len(symbols), _BATCH_SIZE):
        batch = symbols[i:i + _BATCH_SIZE]
        prices = _fetch_batch(batch)
        for sym, data in prices.items():
            try:
                if mode == "open":
                    conn.execute(
                        "INSERT OR REPLACE INTO intraday_snapshot(symbol, date, open_30min, open_30min_vol) "
                        "VALUES (?, ?, ?, ?)",
                        (sym, today, data["price"], data["volume"]))
                else:
                    conn.execute(
                        "UPDATE intraday_snapshot SET close_5min=?, close_5min_vol=? "
                        "WHERE symbol=? AND date=?",
                        (data["price"], data["volume"], sym, today))
                saved += 1
            except Exception:
                errors += 1

    conn.commit()
    conn.close()
    _log.info(f"snapshot {label} done: {saved} saved, {errors} errors")
    return {"saved": saved, "errors": errors}
