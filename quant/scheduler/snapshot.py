"""开盘30分钟/尾盘5分钟价格快照 (test-v324, 修正于 test-v402).

每日 10:00 执行 snapshot_open, 15:00 执行 snapshot_close,
快照所有A股的实时价+量, 供 intraday_reversal / 尾盘异动因子使用.

test-v402: 触发时间从 09:30 修正为 10:00 — 09:30 拉到的是开盘价,
不是开盘30分钟后的价格, 导致因子退化为隔夜缺口因子.
60天积累后激活日内反转因子 (IC_IR≈0.8+, A股最强因子之一).

数据源: 腾讯财经实时行情 (qt.gtimg.cn), 批量拉取.

注意: task_log 由 Runner 统一管理，任务模块不再调用 _tk_start/_tk_finish。
"""
import urllib.request, re, time as _time
from datetime import datetime
from quant.utils.date import today_str
from quant.data.repos._base import DatabaseManager
from quant.utils.logger import get_logger

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
    """批量拉取实时价格+成交量+昨收. 返回 {symbol: {price, volume, prev_close}}.
    v402: 新增 prev_close (fields[4]) — intraday_reversal 因子需要.
    v418 (Bug 6): 列名单位注释 —
      price      元 (fields[3])
      volume     股 (fields[6], 腾讯原始单位=股, 非手; 因子端 _cs_zscore 按截面
                  z-score, 单位恒正缩放不影响秩相关; 若未来需 手 需 ÷100)
      prev_close 元 (fields[4])
    """
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
            prev_close = float(fields[4]) if len(fields) > 4 and fields[4] else 0
            if price <= 0:
                continue
            symbol = m.group(1)[2:]
            results[symbol] = {"price": round(price, 2), "volume": volume, "prev_close": round(prev_close, 2)}
        except (ValueError, IndexError):
            continue
    return results


def snapshot_open(today: str = None):
    """快照开盘30分钟价格+成交量."""
    return _snapshot(today, mode="open")


def snapshot_close(today: str = None):
    """快照尾盘5分钟价格+成交量."""
    return _snapshot(today, mode="close")


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
    conn.execute("PRAGMA journal_mode=WAL")

    saved = 0
    errors = 0
    for i in range(0, len(symbols), _BATCH_SIZE):
        batch = symbols[i:i + _BATCH_SIZE]
        data = _fetch_batch(batch)
        if not data:
            continue
        for sym, val in data.items():
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO intraday_snapshot (date, symbol, mode, price, volume, prev_close) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (today, sym, mode, val["price"], val["volume"], val["prev_close"])
                )
                saved += 1
            except Exception as e:
                errors += 1
                _log.warning(f"snapshot write {sym} failed: {e}")
        conn.commit()
        _time.sleep(0.05)  # 限速

    conn.close()
    _log.info(f"snapshot {label} done: saved={saved} errors={errors}")
    return {"saved": saved, "errors": errors}


if __name__ == "__main__":
    import sys
    mode = sys.argv[2] if len(sys.argv) > 2 else "open"
    _snapshot(sys.argv[1] if len(sys.argv) > 1 else today_str(), mode=mode)