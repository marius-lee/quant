"""实时行情 — 新浪财经免费接口，批量拉取持仓现价。

接口: http://hq.sinajs.cn/list=sh600036,sz000001,bj430047
限制: 单次最多 ~80 只，无认证。
缓存: 3 秒 TTL，前端每秒轮询不重复打新浪。
只在交易日盘中拉取 (9:30-15:00)，盘后/非交易日跳过。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import urllib.request
from datetime import datetime
from config.constants import _require_cfg
from utils.logger import get_logger

logger = get_logger("execution.quote")

_SINA_URL = "http://hq.sinajs.cn/list="
_BATCH_SIZE = 60
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def _symbol_to_sina(symbol: str) -> str:
    """量化代码 → 新浪代码。600036→sh600036, 000001→sz000001, 430047→bj430047"""
    if symbol.startswith(("4", "8", "92")):
        return f"bj{symbol}"
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _parse_sina_line(line: str) -> dict:
    m = re.search(r'hq_str_(\w+)="(.+)"', line)
    if not m:
        return None
    sina_code = m.group(1)
    fields = m.group(2).split(",")
    if len(fields) < 32:
        return None

    try:
        price = float(fields[3]) if fields[3] else 0
        prev_close = float(fields[2]) if fields[2] else 0
    except ValueError:
        return None

    if price <= 0:
        return None

    symbol = sina_code[2:]

    return {
        "symbol": symbol,
        "name": fields[0],
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "change_pct": round((price / prev_close - 1) * 100, 2) if prev_close > 0 else 0,
        "high": round(float(fields[4]) if fields[4] else price, 2),
        "low": round(float(fields[5]) if fields[5] else price, 2),
        "open": round(float(fields[1]) if fields[1] else price, 2),
        "volume": int(float(fields[8])) if len(fields) > 8 and fields[8] else 0,
    }


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """批量拉取实时行情 (模板5: 多批并行 HTTP).

    Args:
        symbols: 股票代码列表，如 ["600036", "000001"]
    Returns:
        {symbol: {name, price, prev_close, change_pct, high, low, open, volume}}
    """
    if not symbols:
        return {}

    result = {}
    batches = [symbols[i:i + _BATCH_SIZE] for i in range(0, len(symbols), _BATCH_SIZE)]

    def _fetch_batch(batch: list[str]) -> dict[str, dict]:
        sina_codes = ",".join(_symbol_to_sina(s) for s in batch)
        url = _SINA_URL + sina_codes
        partial = {}
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.sina")) as resp:
                text = resp.read().decode("gbk")
        except Exception as e:
            logger.warning(f"quote fetch failed: {e}")
            return partial
        for line in text.strip().split("\n"):
            parsed = _parse_sina_line(line)
            if parsed:
                partial[parsed["symbol"]] = parsed
        return partial

    if len(batches) > 1:
        with ThreadPoolExecutor(max_workers=min(len(batches), _require_cfg("execution.quote.max_batch_workers"))) as ex:
            futures = {ex.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
            for f in as_completed(futures):
                result.update(f.result())
    else:
        result = _fetch_batch(batches[0])

    return result


def is_trading_time() -> bool:
    """当前是否在交易时段内 (9:30-15:00 交易日)"""
    try:
        from execution.calendar import is_market_open
        return is_market_open()
    except Exception:
        logger.warning("failed to check trading hours from calendar, using wall-clock fallback")
        now = datetime.now()
        t = now.time()
        import datetime as _dt
        return _dt.time(9, 30) <= t <= _dt.time(15, 0) and now.weekday() < 5
