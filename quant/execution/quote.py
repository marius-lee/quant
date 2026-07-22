"""实时行情 — 腾讯财经为主, 新浪备用。批量拉取持仓现价。

腾讯: http://qt.gtimg.cn/q=sh600036 (主, 单次 ~50 只)
新浪: http://hq.sinajs.cn/list=... (备, 单次 ~80 只)
只在交易日盘中拉取 (9:30-15:00)，盘后/非交易日跳过。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re, urllib.request
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("execution.quote")

_TENCENT_URL = "http://qt.gtimg.cn/q="
_SINA_URL   = "http://hq.sinajs.cn/list="
_BATCH_SIZE = 50

_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com",
}
_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}


# ═══════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════

def fetch_quotes(symbols: list[str], include_ask_bid: bool = False) -> dict[str, dict]:
    """批量拉取实时行情: 腾讯主源, Sina 自动补齐缺失。

    Args:
        symbols: 股票代码列表，如 ["600036", "000001"]
    Returns:
        {symbol: {name, price, prev_close, change_pct, high, low, open, volume}}
    """
    if not symbols:
        return {}
    result: dict[str, dict] = {}
    batches = [symbols[i:i + _BATCH_SIZE] for i in range(0, len(symbols), _BATCH_SIZE)]

    if len(batches) > 1:
        ex = ThreadPoolExecutor(max_workers=min(len(batches), _require_cfg("execution.quote.max_batch_workers")))
        try:
            futures = {ex.submit(_fetch_tencent_batch, b, include_ask_bid): i for i, b in enumerate(batches)}
            for f in as_completed(futures):
                result.update(f.result())
        finally:
            ex.shutdown(wait=False)
    else:
        result = _fetch_tencent_batch(batches[0], include_ask_bid)

    # Sina 备用补齐缺失
    missing = [s for s in symbols if s not in result]
    if missing:
        result.update(_fetch_sina_batch(missing))
    return result


def is_trading_time() -> bool:
    """当前是否在交易时段内 (9:30-15:00 交易日)"""
    from quant.execution.calendar import is_market_open
    return is_market_open()


# ═══════════════════════════════════════
# 腾讯行情 (主)
# ═══════════════════════════════════════

def _symbol_to_tencent(symbol: str) -> str:
    if symbol.startswith(("4", "8", "92")):
        return f"bj{symbol}"
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _parse_tencent_line(line: str, include_ask_bid: bool = False) -> dict | None:
    """v_sh600036="1~茅台~600519~1900.00~..."  →  {symbol, name, price, ...}"""
    m = re.search(r'v_(\w+)="(.+)"', line)
    if not m:
        return None
    fields = m.group(2).split("~")
    if len(fields) < 4:
        return None
    try:
        price = float(fields[3]) if fields[3] else 0
        prev_close = float(fields[4]) if len(fields) > 4 and fields[4] else 0
        if price <= 0:
            return None
        symbol = m.group(1)[2:]  # 去掉 sh/sz/bj 前缀
        result = {
            "symbol": symbol,
            "name": fields[1] if len(fields) > 1 else "",
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round((price / prev_close - 1) * 100, 2) if prev_close > 0 else 0,
            "high": round(float(fields[33]) if len(fields) > 33 and fields[33] else price, 2),
            "low": round(float(fields[34]) if len(fields) > 34 and fields[34] else price, 2),
            "open": round(float(fields[5]) if len(fields) > 5 and fields[5] else price, 2),
            "volume": int(float(fields[6])) if len(fields) > 6 and fields[6] else 0,
        }
        # 五档盘口数据: 腾讯API field 9-10=买一价/量(手), 19-20=卖一价/量(手)
        # 来源: ADR-033 限价单执行, 用于检测涨停封板(ask_volume==0 → 无人卖出)
        if include_ask_bid and len(fields) > 20:
            result["ask"] = round(float(fields[19]), 2) if fields[19] else 0
            result["ask_volume"] = int(float(fields[20])) * 100 if fields[20] else 0  # 手→股
            result["bid"] = round(float(fields[9]), 2) if len(fields) > 9 and fields[9] else 0
            result["bid_volume"] = int(float(fields[10])) * 100 if len(fields) > 10 and fields[10] else 0
        return result
    except (ValueError, IndexError):
        return None


def _fetch_tencent_batch(batch: list[str], include_ask_bid: bool = False) -> dict[str, dict]:
    """腾讯实时行情单批拉取."""
    codes = ",".join(_symbol_to_tencent(s) for s in batch)
    req = urllib.request.Request(_TENCENT_URL + codes, headers=_TENCENT_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.tencent")) as resp:
            text = resp.read().decode("gbk")
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for line in text.strip().split("\n"):
        p = _parse_tencent_line(line, include_ask_bid)
        if p:
            result[p["symbol"]] = p
    return result


# ═══════════════════════════════════════
# 新浪行情 (备)
# ═══════════════════════════════════════

def _symbol_to_sina(symbol: str) -> str:
    if symbol.startswith(("4", "8", "92")):
        return f"bj{symbol}"
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _parse_sina_line(line: str) -> dict | None:
    m = re.search(r'hq_str_(\w+)="(.+)"', line)
    if not m:
        return None
    fields = m.group(2).split(",")
    if len(fields) < 32:
        return None
    price = float(fields[3]) if fields[3] else 0
    prev_close = float(fields[2]) if fields[2] else 0
    if price <= 0:
        return None
    symbol = m.group(1)[2:]
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


def _fetch_sina_batch(batch: list[str]) -> dict[str, dict]:
    """新浪实时行情单批拉取."""
    codes = ",".join(_symbol_to_sina(s) for s in batch)
    req = urllib.request.Request(_SINA_URL + codes, headers=_SINA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.sina")) as resp:
            text = resp.read().decode("gbk")
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for line in text.strip().split("\n"):
        p = _parse_sina_line(line)
        if p:
            result[p["symbol"]] = p
    return result
