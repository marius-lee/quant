"""Data storage helpers + DataStoreHelperMixin.

包含模块级函数 (_ts_code, _bs_socket_timeout, _tencent_market, market_conn)
和 DataStoreHelperMixin (内部辅助方法)。
"""
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.data.repos._base import DatabaseManager

from datetime import datetime
from quant.utils.date import to_str
from quant.utils.date import today_str

logger = get_logger("data.store")

# test-v303
_TICKFLOW_BATCH_NO_PERM = False


def _ts_code(sym: str) -> str:
    """转换股票符号为 tushare 格式 (sh/sz/bj)."""
    if sym.startswith(("4", "8", "92")):
        return f"{sym}.BJ"
    if sym.startswith(("6", "9", "68")):
        return f"{sym}.SH"
    return f"{sym}.SZ"


def _bs_socket_timeout() -> None:
    """baostock 阻塞 socket 强制超时 — login 后调用."""
    try:
        from baostock.common import context as _bsctx
        _sock = getattr(_bsctx, "default_socket", None)
        if _sock is not None:
            _sock.settimeout(_require_cfg("data.http_timeout.baostock"))
    except Exception as _e:
        logger.warning(f"baostock socket timeout setup failed: {_e}")


def _tencent_market(sym: str) -> str:
    """返回腾讯财经行情前缀: sh/sz/bj"""
    if sym.startswith(("4", "8", "92")):
        return "bj"
    if sym.startswith(("6", "9", "68")):
        return "sh"
    return "sz"


def market_conn(mode='ro'):
    """统一数据库连接 — 自动 WAL + busy_timeout=30s.

    mode: 'ro' = read-only, 'rw' = read-write.
    """
    _c = DatabaseManager.get_connection(MARKET_DB)
    _c.execute("PRAGMA journal_mode=WAL")
    _c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    if mode == 'ro':
        _c.execute("PRAGMA read_uncommitted=1")
    return _c


class DataStoreHelperMixin:
    """内部辅助方法 mixin."""

    @staticmethod
    def _norm_row(sym: str, date: str, o: float, h: float, l: float, c: float,
                  vol: float, amt: float, turnover: float = 0.0) -> tuple:
        """标准化一行日线数据: 日期→ISO(YYYY-MM-DD), 成交量→手, 成交额→千元, 精度4位小数。"""
        from quant.utils.date import to_str
        return (sym, to_str(date), round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                round(vol, 4), round(amt, 4), round(turnover, 4))

    def _log_source_sample(self, source: str, rows: list, chunk: list):
        """记录每条数据源的样本值，便于事后排查单位/精度问题。"""
        if not rows:
            return
        sample_sym = chunk[0]
        sample_rows = [r for r in rows if r[0] == sample_sym]
        if sample_rows:
            r = sample_rows[0]
            logger.debug(f"[{source}] sample: {r[0]} {r[1]} O={r[2]} H={r[3]} L={r[4]} "
                        f"C={r[5]} V={r[6]} Amt={r[7]} To={r[8]}")

    def _ts_codes(self, symbols: list) -> list:
        """6位代码 → tushare ts_code (带交易所后缀)。"""
        out = []
        for s in symbols:
            if '.' in s:
                out.append(s)
            elif s.startswith("92"):
                out.append(f"{s}.BJ")
            elif s.startswith(("6", "5", "9")):
                out.append(f"{s}.SH")
            elif s.startswith(("0", "2", "3")):
                out.append(f"{s}.SZ")
        return out

    def _local_qfq_ratio(self, conn, symbols: list) -> tuple:
        """本地因子表 → ({symbol: latest_factor}, {symbol: {date: factor}})。"""
        if not symbols:
            return {}, {}
        self._ensure_adj_factor_tables(conn)
        ph = ",".join("?" for _ in symbols)
        rows = conn.execute(
            f"SELECT symbol, date, factor FROM adj_factor WHERE symbol IN ({ph})",
            tuple(symbols)).fetchall()
        factor_map: dict = {}
        for sym, d, f in rows:
            factor_map.setdefault(sym, {})[d] = f
        latest_map = {s: ds[max(ds)] for s, ds in factor_map.items() if ds}
        return latest_map, factor_map

    def _analyze_daily_gaps(self, conn, target_date: str = None) -> dict:
        """分析日线数据缺口 — missing / stale / stale_recent / full。"""
        from datetime import datetime, timedelta
        stale_days = _require_cfg("data.stale_days")
        cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
        rows = conn.execute("""SELECT symbol, MIN(date), MAX(date) FROM daily GROUP BY symbol ORDER BY symbol""").fetchall()
        global_max = conn.execute("SELECT MAX(date) FROM daily WHERE date >= '2000-01-01' AND date < '2100-01-01'").fetchone()[0] or "2020-01-01"
        today_str = datetime.now().strftime("%Y-%m-%d")
        ref_date = target_date if target_date else today_str
        try:
            from quant.execution.calendar import get_trading_days
            all_td = sorted(get_trading_days())
            most_recent_td = [d for d in all_td if d <= ref_date][-1] if all_td else ref_date
        except Exception:
            most_recent_td = ref_date
        all_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market!='BJ'").fetchall()}
        have_data = set()
        stale, stale_recent, full = [], [], []
        for sym, min_d, max_d in rows:
            have_data.add(sym)
            if sym not in all_symbols:
                continue
            if max_d < cutoff:
                stale.append(sym)
                continue
            if max_d < most_recent_td:
                stale_recent.append(sym)
                continue
            full.append(sym)
        missing = sorted(all_symbols - have_data)
        return {"missing": missing, "stale": stale, "stale_recent": stale_recent, "full": full, "total": len(all_symbols)}


__all__ = ['DataStore', 'market_conn', 'logger',
           'D_DATE', 'D_SYMBOL', 'D_OPEN', 'D_HIGH', 'D_LOW', 'D_CLOSE',
           'D_VOLUME', 'D_AMOUNT', 'D_TURNOVER', 'D_PE_TTM', 'D_PB',
           'D_TOTAL_MV', 'D_CIRC_MV', 'S_SYMBOL', 'S_NAME', 'S_MARKET',
           'S_LIST_DATE', 'S_INDUSTRY', 'F_PE', 'F_PB', 'F_TOTAL_MV',
           'F_CIRC_MV', 'F_ROE', 'F_EPS', 'F_BVPS',
           '_ts_code', '_bs_socket_timeout', '_tencent_market',
           'DataStoreHelperMixin', '_TICKFLOW_BATCH_NO_PERM']
