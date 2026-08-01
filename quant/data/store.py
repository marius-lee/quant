"""SQLite 数据仓库 — 全A股 + 增量更新。
    首次: 下载全部A股列表 + 全部历史日线 → SQLite
    后续: 对比 SQLite 已有数据，只拉取增量日期
"""
import os
import sqlite3
import threading
import time
from datetime import datetime
from quant.utils.date import to_str, to_compact, today_str, DEFAULT_START_DATE

import pandas as pd

from quant.utils.logger import get_logger
logger = get_logger("data.store")

# test-v303: 注册 key 无批量K线权限 (tickflow.PermissionError) 时置 True,
# 进程内不再白试注册端; 新进程/升级套餐后自动重试。
_TICKFLOW_BATCH_NO_PERM = False

from quant.data.cache import get_backend, DataCache, RateLimiter
from quant.config.loader import load as _load_config
from quant.config.constants import _require_cfg
from quant.data.repos._base import DatabaseManager
# ── 列名常量 (DDL 与查询共引，防 value→raw_value 类脱节) ──
# daily 表
D_DATE     = "date"
D_SYMBOL   = "symbol"
D_OPEN     = "open"
D_HIGH     = "high"
D_LOW      = "low"
D_CLOSE    = "close"
D_VOLUME   = "volume"
D_AMOUNT   = "amount"
D_TURNOVER = "turnover"
D_PE_TTM   = "pe_ttm"
D_PB       = "pb"
D_TOTAL_MV = "total_mv"
D_CIRC_MV  = "circ_mv"

# stocks 表
S_SYMBOL    = "symbol"
S_NAME      = "name"
S_MARKET    = "market"
S_LIST_DATE = "list_date"
S_INDUSTRY  = "industry"

# fundamentals 附加列 (通过 ALTER TABLE 添加)
F_PE       = "pe"
F_PB       = "pb"
F_TOTAL_MV = "total_mv"
F_CIRC_MV  = "circ_mv"
F_ROE      = "roe"
F_EPS      = "eps"
F_BVPS     = "bvps"
from quant.config.paths import MARKET_DB
from quant.utils.date import validate_date_format

# ── Module-level cache (lazy init) ──
_backend = None
_stock_list_cache = None
_industry_cache = None
_tushare_limiter = None
_akshare_limiter = None

def _init_cache():
    global _backend, _stock_list_cache, _industry_cache, _tushare_limiter, _akshare_limiter
    if _backend is not None:
        return
    cfg = _load_config()
    _backend = get_backend(cfg)
    _stock_list_cache = DataCache("store:stock_list", ttl_hours=24, backend=_backend)
    _industry_cache = DataCache("store:industry", ttl_hours=24, backend=_backend)
    _tushare_limiter = RateLimiter("tushare", calls_per_minute=_require_cfg("data.rate_limit.tushare_calls_per_minute"), burst=2, backend=_backend)  # burst=2: 防初始爆发触发服务端封禁 (来源: 2026-07-21 根因分析)  # 来源: config.yaml data.rate_limit.tushare_calls_per_minute
    _akshare_limiter = RateLimiter("akshare", calls_per_minute=_require_cfg("data.rate_limit.akshare_calls_per_minute"), burst=2, backend=_backend)  # burst=2 同上  # 来源: config.yaml data.rate_limit.akshare_calls_per_minute
    logger.debug("cache layer initialized (backend=%s)", type(_backend).__name__)

def _ts_code(sym: str) -> str:
    # 北交所优先判断（92开头必须以"92"先匹配，避免被"9"捕获）
    if sym.startswith(("4", "8", "92")):
        return f"{sym}.BJ"
    if sym.startswith(("6", "9", "68")):
        return f"{sym}.SH"
    return f"{sym}.SZ"


def _tencent_market(sym: str) -> str:
    """返回腾讯财经行情前缀: sh/sz/bj"""
    if sym.startswith(("4", "8", "92")):
        return "bj"
    if sym.startswith(("6", "9", "68")):
        return "sh"
    return "sz"


class DataStore:
    """全A股 SQLite 数据仓库 — 单连接复用，任务结束时关闭。"""

    def __init__(self, db_path: str = MARKET_DB,
                 tushare_token: str = None):
        self.db_path = db_path
        # tushare token 优先级: 显式传参 > 环境变量 > config.yaml (来源: HANDOFF test-v168)
        _token = tushare_token
        if not _token:
            _token = os.environ.get("TUSHARE_TOKEN", "")
        if not _token:
            _token = _require_cfg("data.tushare_token")
        self.token = _token
        self._conn = None
        self._local = threading.local()  # thread-local connections for WAL concurrent reads
        self._lock = threading.Lock()     # guard shared _conn creation (P71)
        self._query_cache: dict = {}  # LRU query cache per DataStore instance
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol    TEXT PRIMARY KEY,
                name      TEXT,
                market    TEXT,
                list_date TEXT,
                industry  TEXT
            );
            CREATE TABLE IF NOT EXISTS daily (
                symbol   TEXT,
                date     TEXT,
                open     REAL,
                high     REAL,
                low      REAL,
                close    REAL,
                volume   REAL,
                amount   REAL,
                turnover REAL,
                PRIMARY KEY (symbol, date)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
            CREATE INDEX IF NOT EXISTS idx_stocks_market_sym ON stocks(market, symbol);
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS lhb_detail (
                symbol   TEXT,
                trade_date TEXT,
                close     REAL,
                change_pct REAL,
                turnover_rate REAL,
                net_buy    REAL,
                buy_amt   REAL,
                sell_amt  REAL,
                reason    TEXT,
                PRIMARY KEY (symbol, trade_date)
            );
            CREATE TABLE IF NOT EXISTS daily_valuation (
                symbol TEXT,
                date TEXT,
                pe_ttm REAL,
                pb REAL,
                ps_ttm REAL,
                pcf_ttm REAL,
                market_cap REAL,
                turnover_rate REAL,
                source TEXT DEFAULT 'jqdata',
                PRIMARY KEY (symbol, date)

            );
        """)
        conn.commit()
        # 为基本面因子添加列 (安全迁移, 列已存在时不报错)
        fund_cols = [
            ("pe", "REAL"), ("pb", "REAL"), ("total_mv", "REAL"),
            ("roe", "REAL"), ("high_52w", "REAL"), ("low_52w", "REAL"),
            ("circ_mv", "REAL"), ("eps", "REAL"), ("bvps", "REAL"),
            ("div_yield", "REAL"), ("turnover_rate", "REAL"),
            ("pe_ttm", "REAL"), ("cfps", "REAL"),
        ]
        # ── Gap 4: survivorship bias — delisted stock tracking ──
        try:
            conn.execute("ALTER TABLE stocks ADD COLUMN delist_date TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE stocks ADD COLUMN list_status TEXT DEFAULT 'L'")
        except sqlite3.OperationalError:
            pass

        for col, typ in fund_cols:
            try:
                conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()

    def _connect(self):
        """获取线程局部连接。每线程独立 sqlite3 连接，支持 WAL 并发读。
        
        保持 _conn 向后兼容（单线程调用者），同时为多线程场景提供 _local.conn。
        线程安全：_lock 保护 shared _conn 的创建，避免多线程竞态条件（P71）。
        """
        with self._lock:
            if self._conn is None:
                self._conn = self._make_conn()
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = self._make_conn()
        return self._local.conn

    def _make_conn(self):
        """创建新的 sqlite3 连接（WAL + 性能调优）。"""
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        c.execute("PRAGMA cache_size=-64000")
        return c

    def close(self):
        """关闭所有线程局部连接 + 主连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    # ============================================================
    # 股票列表
    # ============================================================

    def sync_stock_list(self) -> int:
        """拉取全A股列表。优先 tushare，失败回退 akshare（免费无频率限制）。"""
        _init_cache()
        conn = self._connect()
        existing = set(
            r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()
        )

        # 1. Cache check — skip API if fresh data in local cache
        cached = _stock_list_cache.get("symbols")
        if cached is not None and isinstance(cached, list) and len(cached) > 0:
            insert_count = 0
            for item in cached:
                sym = item.get("symbol", item.get("code", ""))
                if not sym or len(str(sym)) != 6:
                    continue
                if sym not in existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                        (sym, item.get("name", ""), item.get("market", ""), item.get("list_date", "")))
                    insert_count += 1
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
            logger.info(f"stock list (cache hit): {total} total ({insert_count} new)")
            return total

        # 尝试 tushare
        if self.token:
            import tushare as ts
            ts.set_token(self.token)
            pro = ts.pro_api()
            _tushare_limiter.wait()
            df = pro.stock_basic(exchange="", list_status="L",
                fields="ts_code,symbol,name,list_date,market")
            if df is not None and not df.empty:
                # cache the raw response
                _stock_list_cache.put("symbols", df.to_dict(orient="records"))
                for _, row in df.iterrows():
                    sym = row["symbol"]
                    exchange = row.get("market", "")
                    if exchange == "SHSE": market = "SH"
                    elif exchange == "SZSE": market = "SZ"
                    elif exchange == "BJSE": market = "BJ"
                    else: market = "SH"
                    if sym not in existing:
                        conn.execute(
                            "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                            (sym, row["name"], market, row.get("list_date", "")))
                conn.commit()
                total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
                logger.info(f"stock list (tushare): {total} total")
                return total
        import akshare as ak
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _fetch_stock_list():
            return ak.stock_info_a_code_name()

        df = _fetch_stock_list()
        new_count = 0
        for _, row in df.iterrows():
            sym = str(row.get("code", row.get("item_code", ""))).zfill(6)
            name = row.get("name", "")
            if sym not in existing and len(sym) == 6:
                if sym.startswith(("4", "8", "92")):
                    market = "BJ"
                elif sym.startswith(("6","9","68")):
                    market = "SH"
                else:
                    market = "SZ"
                conn.execute(
                    "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                    (sym, name, market, ""))
                new_count += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        logger.info(f"stock list (akshare): {total} total ({new_count} new)")
        return new_count
    def sync_delisted_stocks(self) -> int:
        """Pull delisted stocks from akshare and add to stocks table.

        Sets list_status='D' and delist_date for historical stocks that
        have been delisted. Their daily data (while listed) is pulled
        by update_daily normally.
        """
        conn = self._connect()
        existing = set(
            r[0] for r in conn.execute(
                "SELECT symbol FROM stocks WHERE list_status='D'"
            ).fetchall()
        )
        import akshare as ak
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _fetch_delist():
            try:
                return ak.stock_info_a_delist()
            except AttributeError:
                import pandas as _pd2
                df_sh = ak.stock_info_sh_delist()
                df_sz = ak.stock_info_sz_delist()
                return _pd2.concat([df_sh, df_sz], ignore_index=True)

        df = _fetch_delist()
        if df is None or df.empty:
            return 0
        new_count = 0
        for _, row in df.iterrows():
            sym = str(row.get("symbol", row.get("code", ""))).zfill(6)
            name = row.get("name", "")
            delist_d = str(row.get("delist_date", row.get("delisting_date", "")))[:10]
            if len(sym) != 6 or sym in existing:
                continue
            if sym.startswith(("6", "9", "68")):
                mkt = "SH"
            elif sym.startswith(("4", "8", "92")):
                mkt = "BJ"
            else:
                mkt = "SZ"
            conn.execute(
                "INSERT OR REPLACE INTO stocks(symbol, name, market, "
                "list_status, delist_date) VALUES(?,?,?,?,?)",
                (sym, name, mkt, "D", delist_d))
            new_count += 1
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE list_status='D'"
        ).fetchone()[0]
        logger.info(
            f"delisted sync: {new_count} new ({total} total delisted)")
        return new_count
    def get_universe(self, date_str: str = None):
        """Get point-in-time stock universe for a given date.

        Only includes stocks that were:
          - Listed on or before date_str
          - Not yet delisted (delist_date is NULL or after date_str)
          - Non-Beijing Exchange

        This eliminates survivorship bias in backtesting.
        """
        conn = self._connect()
        query = (
            "SELECT symbol FROM stocks "
            "WHERE list_date <= ? "
            "  AND (delist_date IS NULL OR delist_date > ?) "
            "  AND market != 'BJ'"
        )
        if date_str is None:
            from datetime import date
            date_str = date.today().strftime("%Y-%m-%d")
        rows = conn.execute(query, (date_str, date_str)).fetchall()
        return [r[0] for r in rows]

    def sync_industry(self):
        """拉取行业分类 — baostock 证监会行业分类 (0.9.20 起支持 Python 3.14; akshare 回退)。

        注意: baostock ≥0.9.20 已实测兼容 Python 3.14 (2026-07-26 纠偏, 旧注释过时)。
        数据已分类时直接跳过。
        """
        _init_cache()
        conn = self._connect()
        try:
            conn.execute("ALTER TABLE stocks ADD COLUMN industry TEXT")
        except sqlite3.OperationalError:
            pass
        classified = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        if classified >= total:
            logger.info(f"industry sync skipped: {classified}/{total} already classified")
            return 0

        # 1. Cache check
        cached = _industry_cache.get("mapping")
        if cached is not None and isinstance(cached, dict):
            updated = 0
            for sym, ind in cached.items():
                conn.execute(
                    "UPDATE stocks SET industry=? WHERE symbol=? AND industry IS NULL",
                    (ind, sym))
                updated += conn.total_changes
            conn.commit()
            logger.info(f"industry sync (cache hit): {updated} updates")
            return updated

        # baostock attempt
        try:
            import baostock as bs
        except ImportError:
            logger.info("baostock library not installed, trying akshare...")
            return self._sync_industry_akshare(conn)
        bs.login()
        rs = bs.query_stock_industry()
        df = rs.get_data()
        bs.logout()
        if df.empty:
            return 0
        # build cache mapping: symbol -> industry
        industry_map = {}
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            sym = code.split(".")[-1] if "." in code else code
            ind = str(row.get("industry", "")).strip()
            if len(sym) == 6 and ind:
                industry_map[sym] = ind
        _industry_cache.put("mapping", industry_map)

        updated = 0
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            ind = str(row.get("industry", "")).strip()
            if not ind:
                continue
            sym = code.split(".")[-1] if "." in code else code
            if len(sym) != 6:
                continue
            conn.execute(
                "UPDATE stocks SET industry=? WHERE symbol=? AND industry IS NULL",
                (ind, sym)
            )
            updated += 1
        conn.commit()
        classified = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        logger.info(f"industry sync done (baostock): {updated} updates, {classified}/{total}")
        return updated
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
        # 取本批第一只股票的样本
        sample_sym = chunk[0]
        sample_rows = [r for r in rows if r[0] == sample_sym]
        if sample_rows:
            r = sample_rows[0]
            logger.debug(f"[{source}] sample: {r[0]} {r[1]} O={r[2]} H={r[3]} L={r[4]} "
                        f"C={r[5]} V={r[6]} Amt={r[7]} To={r[8]}")

    # ═══════════════════════════════════════════════════════
    # 复权因子本地表 (报告 §3.3 / B-08 v2)
    # tushare adj_factor 接口限流 1次/小时, daily 接口 200次/分钟。
    # 因子只在除权日变化 → 落本地表, 写 daily 时用本地因子转 qfq,
    # 完全绕开限流。因子表由 sync_adj_factor 后台低频填充 (hourly cron)。
    # ═══════════════════════════════════════════════════════

    def _ensure_adj_factor_tables(self, conn):
        """adj_factor (复权因子) + adj_factor_state (重基准状态) 建表 (幂等)。"""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS adj_factor (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                factor REAL NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (symbol, date)
            );
            CREATE TABLE IF NOT EXISTS adj_factor_state (
                symbol TEXT PRIMARY KEY,
                latest_factor REAL NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)

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

    def sync_adj_factor(self, max_batches: int = 1, batch_size: int = 50) -> dict:
        """同步全市场复权因子 (adj_factor) 落本地表。

        双源策略:
          1. tushare adj_factor 接口 — 批量 50 股, 速度快但限流极严 (免费档 ~3-4次/天).
          2. baostock query_adjust_factor — 逐只拉取, 无公开限流, tushare 限流后自动接盘.

        选股顺序: 本地无因子的优先, 其次按 updated_at 最旧 (维护模式).
        每批成功后自动 rebase 因子跳变股票的 daily 历史 (除权重基准).

        tushare 铺满: ~108 批 × 50 股, 但受限于 ~3-4 批/天 → 需 30+ 天.
        baostock 铺满: 5481 股 × ~0.3s/只 ≈ 27 分钟 (无严格限流).
        设计: cron 每小时调一次, tushare 限流后 baostock 补位.

        返回: {'batches': k, 'rows': n, 'rate_limited': bool, 'remaining': m,
                'source': 'tushare'|'baostock'|'mixed'}
        """
        conn = self._connect()
        self._ensure_adj_factor_tables(conn)
        # 本地无因子或最久未更新的股票优先
        pending = [r[0] for r in conn.execute("""
            SELECT s.symbol FROM stocks s
            LEFT JOIN (
                SELECT symbol, MAX(updated_at) AS mu FROM adj_factor GROUP BY symbol
            ) f ON f.symbol = s.symbol
            WHERE s.symbol NOT LIKE 'BJ%'
            ORDER BY f.mu IS NOT NULL, f.mu
        """).fetchall()]
        if not pending:
            logger.info("sync_adj_factor: all symbols covered")
            return {"batches": 0, "rows": 0, "rate_limited": False, "remaining": 0,
                    "source": "none"}

        start = to_compact(_require_cfg("data.start_date"))
        source_used = "none"
        total_rows, batches, rate_limited = 0, 0, False

        # ── 阶段 1: tushare 批量拉取 (首选, 批量 50 股) ──
        if self.token:
            import tushare as ts
            ts.set_token(self.token)
            pro = ts.pro_api(timeout=_require_cfg("data.http_timeout.tushare"))
            _init_cache()  # 初始化 _tushare_limiter

            for bi in range(0, min(len(pending), max_batches * batch_size), batch_size):
                chunk = pending[bi:bi + batch_size]
                codes = self._ts_codes(chunk)
                if not codes:
                    continue
                _tushare_limiter.wait()
                try:
                    fdf = pro.adj_factor(
                        ts_code=",".join(codes),
                        start_date=start,
                        end_date=to_compact(datetime.today()),
                        fields="ts_code,trade_date,adj_factor",
                    )
                except Exception as e:
                    msg = str(e)
                    if "频率超限" in msg or "freq" in msg.lower() or "限" in msg:
                        logger.warning(
                            f"sync_adj_factor: tushare rate limited at batch {bi // batch_size} "
                            f"(免费档 ~3-4批/天, cron 每小时触发一次, 限流属正常); "
                            f"→ 回退 baostock")
                        rate_limited = True
                        break
                    logger.warning(f"sync_adj_factor: tushare batch {bi // batch_size} failed: {e}")
                    continue
                if fdf is None or fdf.empty:
                    logger.warning(f"sync_adj_factor: tushare empty factor for batch {bi // batch_size}")
                    continue
                rows = [
                    (r["ts_code"].split(".")[0],
                     f"{r['trade_date'][:4]}-{r['trade_date'][4:6]}-{r['trade_date'][6:]}",
                     float(r["adj_factor"]))
                    for _, r in fdf.iterrows()
                    if r.get("adj_factor") is not None
                ]
                conn.executemany(
                    "INSERT INTO adj_factor (symbol, date, factor) VALUES (?,?,?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET factor=excluded.factor, "
                    "updated_at=datetime('now','localtime')",
                    rows)
                conn.commit()
                total_rows += len(rows)
                batches += 1
                source_used = "tushare"
                logger.info(f"sync_adj_factor: [tushare] batch {bi // batch_size} {len(chunk)} symbols, "
                            f"{len(rows)} rows (total {total_rows})")
                rebased = self._rebase_ex_dividend(conn, chunk)
                if rebased:
                    logger.info(f"sync_adj_factor: rebased {rebased} ex-dividend symbols")

        # ── 阶段 2: baostock 逐只兜底 (tushare 限流或无 token 时) ──
        processed_tushare = batches * batch_size
        remaining_stocks = pending[processed_tushare:]
        if (rate_limited or not self.token) and remaining_stocks:
            bs_batches, bs_rows = self._sync_adj_factor_baostock(
                conn, remaining_stocks[:max_batches * batch_size], start)
            if bs_rows > 0:
                total_rows += bs_rows
                batches += bs_batches
                source_used = "mixed" if source_used == "tushare" else "baostock"
                logger.info(f"sync_adj_factor: [baostock] {bs_batches} stocks, "
                            f"{bs_rows} rows (total {total_rows})")

        remaining = len(pending) - (batches * batch_size if source_used == "tushare"
                                    else processed_tushare + len(remaining_stocks[:max_batches * batch_size]))
        return {"batches": batches, "rows": total_rows,
                "rate_limited": rate_limited, "remaining": max(remaining, 0),
                "source": source_used}

    def _sync_adj_factor_baostock(self, conn, symbols: list, start: str) -> tuple:
        """baostock (证券宝) 复权因子同步 — 逐只拉取, 无公开限流.

        baostock query_adjust_factor 返回字段:
          - code:              股票代码 (如 sz.000001)
          - dividOperateDate:  除权除息日 (YYYY-MM-DD)
          - foreAdjustFactor:  前复权因子
          - backAdjustFactor:  后复权因子
          - adjustFactor:      复权因子 (本表使用此字段, 与 tushare adj_factor 语义一致)

        baostock 是免费开源 Python 包, 由证券宝 (www.baostock.com) 提供,
        数据来自交易所公开信息, 无 API key, 无严格频率限制.
        单个股票全历史 ~10 条记录 (仅在除权日变化), 非常轻量.
        已在 Python 3.14 (baostock ≥0.9.20) 实测兼容.

        限流/超时对策:
          - 每 200 只自动重登, 防 session 超时 (baostock 免费服务 ~1-2h, 同 backfill_turnover)
          - 每 50 只进度日志 + 速率 ETA
          - 逐只间隔 0.15s, 避免压垮 baostock 服务器
          - 重登失败不中断, 继续下一只

        来源: 2026-07-30 — tushare adj_factor 免费档限流 ~3-4批/天,
              5481 股需 36 天铺满, 改为 baostock ~27 分钟铺满.
        """
        import time as _time

        try:
            import baostock as bs
        except ImportError:
            logger.warning("baostock not installed, skip adj_factor fallback")
            return 0, 0

        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"baostock login failed: {lg.error_msg}")
            return 0, 0

        total_rows, stock_count, processed = 0, 0, 0
        t0 = _time.time()
        total = len(symbols)
        end_date = datetime.today().strftime("%Y-%m-%d")
        done_symbols = []  # 成功写入因子的股票代码, 用于最后 rebase

        def _baostock_code(sym: str) -> str:
            """6位数代码 → baostock 格式. sh.6xxxxx/sh.9xxxxx, sz.0xxxxx/sz.2xxxxx/sz.3xxxxx."""
            if sym.startswith(("6", "9")):
                return f"sh.{sym}"
            return f"sz.{sym}"

        try:
            for sym in symbols:
                bs_code = _baostock_code(sym)

                # ── 重登: 每 200 只防 session 超时 ──
                if processed > 0 and processed % 200 == 0:
                    logger.info(f"baostock adj_factor: re-login at {processed} stocks "
                                f"(防 session 超时, baostock 免费服务 ~1-2h)")
                    bs.logout()
                    lg = bs.login()
                    if lg.error_code != "0":
                        logger.warning(f"baostock re-login failed at {processed}: {lg.error_msg}; "
                                       "continuing with old session")
                    else:
                        _time.sleep(0.5)  # 重登后稍等

                try:
                    rs = bs.query_adjust_factor(
                        code=bs_code, start_date=start, end_date=end_date)
                except Exception as e:
                    logger.warning(f"baostock adj_factor {bs_code}: query failed: {e}")
                    processed += 1
                    continue

                if rs.error_code != "0":
                    processed += 1
                    continue  # 无因子记录 (如新股) — 静默跳过

                stock_rows = 0
                while rs.next():
                    row_data = rs.get_row_data()
                    # row_data: [code, dividOperateDate, foreAdjustFactor,
                    #            backAdjustFactor, adjustFactor]
                    date_str = row_data[1]
                    try:
                        factor_val = float(row_data[4]) if row_data[4] else None
                    except (ValueError, TypeError):
                        factor_val = None
                    if factor_val is None:
                        continue
                    conn.execute(
                        "INSERT INTO adj_factor (symbol, date, factor) VALUES (?,?,?) "
                        "ON CONFLICT(symbol, date) DO UPDATE SET factor=excluded.factor, "
                        "updated_at=datetime('now','localtime')",
                        (sym, date_str, factor_val))
                    stock_rows += 1

                if stock_rows > 0:
                    conn.commit()
                    total_rows += stock_rows
                    stock_count += 1
                    done_symbols.append(sym)

                processed += 1

                # ── 进度日志: 每 50 只 ──
                if processed % 50 == 0:
                    elapsed = _time.time() - t0
                    rate = processed / elapsed if elapsed > 0 else 0
                    eta = (total - processed) / rate if rate > 0 else 0
                    logger.info(f"baostock adj_factor: {processed}/{total} "
                                f"({100*processed//total}%) {total_rows} rows | "
                                f"{elapsed:.0f}s ETA {eta:.0f}s")

                # 逐只间隔 0.15s: baostock 免费服务器无公开限流,
                # 但避免短时间内大量 TCP 连接压垮对方
                _time.sleep(0.15)

            # ── 重基准: 因子落地的股票重写 daily 历史 ──
            if done_symbols:
                rebased = self._rebase_ex_dividend(conn, done_symbols)
                if rebased:
                    logger.info(f"sync_adj_factor: [baostock] rebased {rebased} ex-dividend symbols")

        finally:
            bs.logout()

        elapsed = _time.time() - t0
        logger.info(f"baostock adj_factor done: {stock_count} stocks, {total_rows} rows, "
                    f"{elapsed:.0f}s ({elapsed/60:.1f}min)")
        return stock_count, total_rows

    def _rebase_ex_dividend(self, conn, symbols: list = None) -> int:
        """除权重基准: 因子最新值与 state 不一致的股票, daily 全历史 × F_old/F_new。

        推导: stored_old = raw × f/F_old, 目标 stored_new = raw × f/F_new
              → stored_new = stored_old × F_old/F_new (全历史统一乘, 一条 UPDATE)。
        symbols=None 时处理全表; state 无记录的股票只建档不重写 (历史口径由全量 resync 保证)。
        """
        self._ensure_adj_factor_tables(conn)
        where = ""
        params = ()
        if symbols:
            where = f"WHERE f.symbol IN ({','.join('?' for _ in symbols)})"
            params = tuple(symbols)
        latest = conn.execute(f"""
            SELECT f.symbol, f.factor FROM adj_factor f
            JOIN (SELECT symbol, MAX(date) AS md FROM adj_factor GROUP BY symbol) m
              ON m.symbol = f.symbol AND m.md = f.date
            {where}
        """, params).fetchall()
        rebased = 0
        for sym, f_new in latest:
            st = conn.execute(
                "SELECT latest_factor FROM adj_factor_state WHERE symbol=?",
                (sym,)).fetchone()
            if st is None:
                conn.execute(
                    "INSERT OR IGNORE INTO adj_factor_state (symbol, latest_factor) VALUES (?,?)",
                    (sym, f_new))
                continue
            f_old = float(st[0])
            if f_old > 0 and abs(f_new / f_old - 1) > 1e-6:
                ratio = f_old / f_new
                conn.execute(
                    "UPDATE daily SET open=round(open*?,4), high=round(high*?,4), "
                    "low=round(low*?,4), close=round(close*?,4) WHERE symbol=?",
                    (ratio, ratio, ratio, ratio, sym))
                conn.execute(
                    "UPDATE adj_factor_state SET latest_factor=?, "
                    "updated_at=datetime('now','localtime') WHERE symbol=?",
                    (f_new, sym))
                rebased += 1
                logger.info(f"rebase: {sym} factor {f_old:.4f}→{f_new:.4f}, "
                            f"history × {ratio:.6f}")
        conn.commit()
        return rebased

    def _local_qfq_ratio(self, conn, symbols: list) -> tuple:
        """本地因子表 → ({symbol: latest_factor}, {symbol: {date: factor}})。

        返回 (latest_map, factor_map); 无本地因子的股票不在 map 中。
        """
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

    def _fetch_batch_tushare(self, symbols: list, start_date: str) -> list:
        """tushare 批量获取日线 (Token认证, 200call/min). 返回 None 表示不可用。

        fields 必须显式指定 — tushare pro.daily() 默认字段不含 turnover_rate,
        且当前 tushare 版本不传 fields 时返回空 DataFrame。
        start_date 统一转 YYYYMMDD — tushare 不接受 YYYY-MM-DD 格式。
        来源: 2026-07-21 debug_tushare_fields.py 实测
        """
        if not self.token:
            return None
        import tushare as ts
        ts.set_token(self.token)
        pro = ts.pro_api(timeout=_require_cfg("data.http_timeout.tushare"))  # 来源: config.yaml
        ts_codes_parts = []
        for s in symbols:
            if s.startswith("92"):
                ts_codes_parts.append(f"{s}.BJ")
            elif s.startswith(("6", "5", "9")):
                ts_codes_parts.append(f"{s}.SH")
            elif s.startswith(("0", "2", "3")):
                ts_codes_parts.append(f"{s}.SZ")
        if not ts_codes_parts:
            return None
        code_str = ",".join(ts_codes_parts)

        _init_cache()
        _tushare_limiter.wait()
        # start_date 统一转 YYYYMMDD — tushare 不接受 YYYY-MM-DD (实测返回空)
        _start = to_compact(start_date)  # 统一转 YYYYMMDD (来源: date.py 策略)
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _call_tushare(code_str, start_date, end_date):
            return pro.daily(
                ts_code=code_str,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )

        df = _call_tushare(code_str, _start, to_compact(datetime.today()))
        if df is None or df.empty:
            return None
        # B-08 fix: tushare daily 返回未复权原始价, 与 tencent/akshare 的 qfq
        # 前复权混写同一张表 → 除权日收益率跳变 (如 -34%), 回测不可复现。
        # B-08 v2: 转 qfq 用本地 adj_factor 表 (sync_adj_factor 后台低频填充),
        # 不再在线调 adj_factor 接口 (限流 1次/小时, 每次 update_daily 都调必然超限)。
        # 本地无因子覆盖的股票跳过 (不写口径不一致数据); 全缺 → None 交给下一源。
        _conn = self._connect()
        _latest_map, _factor_map = self._local_qfq_ratio(_conn, symbols)
        _covered = set(_latest_map)
        if not _covered:
            logger.warning("[tushare] no local adj_factor coverage — run "
                           "sync_adj_factor first; skip raw write, next source")
            return None
        df["symbol6"] = df["ts_code"].str.split(".").str[0]
        df = df[df["symbol6"].isin(_covered)]
        if df.empty:
            return None
        _d_iso = df["trade_date"].str[:4] + "-" + df["trade_date"].str[4:6] + "-" + df["trade_date"].str[6:]
        df["adj_factor"] = [
            _factor_map.get(s, {}).get(d)
            for s, d in zip(df["symbol6"], _d_iso)
        ]
        # 同股票内前后填充 (停牌日无因子记录), 仍缺失则该股当天不复权 (ratio=1)
        df["adj_factor"] = df.groupby("symbol6")["adj_factor"].transform(
            lambda s: s.ffill().bfill())
        # 全 None (K线日期与因子日期零重叠) → to_numeric 转 NaN, 防 object/除法 TypeError
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        _ratio = (df["adj_factor"] / df["symbol6"].map(_latest_map)).fillna(1.0)
        for _col in ("open", "high", "low", "close"):
            df[_col] = (df[_col].astype(float) * _ratio).round(4)

        rows = []
        for _, row in df.iterrows():
            rows.append(self._norm_row(
                row["symbol6"], row["trade_date"],
                float(row.get("open", 0)), float(row.get("high", 0)),
                float(row.get("low", 0)), float(row.get("close", 0)),
                float(row.get("vol", 0)), float(row.get("amount", 0)),
                float(0.0)))  # tushare daily API 不含 turnover_rate (来源: 2026-07-21 实测)
        logger.info(f"[tushare] {code_str}: {len(rows)} rows "
                    f"(qfq via local factors, {len(_covered)}/{len(symbols)} covered)")
        return rows

    def _fetch_sina_daily(self, symbols: list, start_date: str) -> list:
        """新浪日线: 收盘后即用(15:30), 免费无需注册, vol=股→/100→手, amt=元"""
        import urllib.request, json as _json
        rows = []
        for sym in symbols:
            if sym.startswith('920'): code = f"bj{sym}"        # BSE 北京交易所 (来源: Sina API bj前缀)
            elif sym.startswith(('6','9')): code = f"sh{sym}"  # 上海
            else: code = f"sz{sym}"                             # 深圳
            url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&datalen=2000"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn",
            })
            data = _json.loads(urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.sina")).read().decode("utf-8"))
            for bar in data:
                d = bar["day"]
                if d < start_date:
                    continue
                rows.append((sym, d,
                    float(bar["open"]), float(bar["high"]),
                    float(bar["low"]), float(bar["close"]),
                    round(float(bar["volume"]) / 100),  # 股→手
                    round(float(bar["volume"]) * float(bar["close"]) / 1000),  # 成交额(千元)
                    float(bar.get("turnover", 0) or 0)))  # 换手率(仅部分股票有)
            import time as _time
            _time.sleep(_require_cfg("data.rate_limit.sina_per_stock_sec"))
        return rows

    def _fetch_baostock_daily(self, symbols: list, start_date: str) -> list:
        """baostock (证券宝) 日线: qfq 前复权, vol=股→手, amt=元→千元, turnover✅.

        baostock 免费开源, 无 API key, 无严格限流. 用 query_history_k_data_plus
        接口拉取前复权日线 (adjustflag=2), 数据质量可靠但逐只拉取 (0.3s/只).
        适合作为 tushare/zzshare/pytdx 之后的兜底源.

        baostock 符号格式: sh.6xxxxx/sh.9xxxxx, sz.0xxxxx/sz.2xxxxx/sz.3xxxxx.
        已在 Python 3.14 (baostock ≥0.9.20) 实测兼容.
        来源: 2026-07-30 — akshare IP 封禁, baostock 补位 OHLCV 兜底.
        """
        import time as _time
        try:
            import baostock as bs
        except ImportError:
            return None

        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"baostock login failed: {lg.error_msg}")
            return None

        rows = []
        try:
            for i, sym in enumerate(symbols):
                # 6位数代码 → baostock 格式
                if sym.startswith(("6", "9")):
                    bs_code = f"sh.{sym}"
                else:
                    bs_code = f"sz.{sym}"

                try:
                    rs = bs.query_history_k_data_plus(
                        code=bs_code,
                        fields="date,open,high,low,close,volume,amount,turn",
                        start_date=start_date,
                        end_date=datetime.today().strftime("%Y-%m-%d"),
                        frequency="d",
                        adjustflag="2",  # 2=前复权
                    )
                except Exception as e:
                    logger.warning(f"baostock daily {bs_code}: query failed: {e}")
                    continue

                if rs.error_code != "0":
                    continue

                while rs.next():
                    row_data = rs.get_row_data()
                    # row_data: [date, open, high, low, close, volume, amount, turn]
                    try:
                        d = row_data[0]
                        o = float(row_data[1])
                        h = float(row_data[2])
                        l = float(row_data[3])
                        c = float(row_data[4])
                        vol = float(row_data[5]) / 100.0  # 股→手
                        amt = float(row_data[6]) / 1000.0  # 元→千元
                        turnover = float(row_data[7]) if row_data[7] else 0.0
                    except (ValueError, IndexError, TypeError):
                        continue
                    rows.append(self._norm_row(sym, d, o, h, l, c, vol, amt, turnover))

                _time.sleep(0.15)  # 逐只间隔, 避免压垮服务器
        finally:
            bs.logout()

        return rows if rows else None

    def _fetch_tencent_daily(self, symbols: list, start_date: str) -> list:
        """东方财富 K线: vol=手, amt=元→/1000→千元.

        TLS 指纹对抗: 使用 curl_cffi 模拟 Chrome 131, 绕过 eastmoney CDN 的 JA3 检测。
        域名从 82.push2his 迁移到 push2.eastmoney.com (82 子域被定向 DNS 封禁)。
        来源: 2026-07-20 Python requests → RemoteDisconnected, curl 同机正常 → TLS 指纹封禁。
        """
        import curl_cffi.requests as _req, json as _json
        rows = []
        end_date = str(datetime.today().strftime("%Y-%m-%d"))
        _session = _req.Session()
        for sym in symbols:
            code = f"1.{sym}" if sym.startswith("6") else f"0.{sym}"
            try:
                r = _session.get(
                    "https://push2.eastmoney.com/api/qt/stock/kline/get",
                    params={
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                        "ut": "7eea3edcaed734bea9cbfc24409ed989",
                        "klt": "101", "fqt": "1", "secid": code,
                        "beg": to_compact(start_date), "end": to_compact(end_date),
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=_require_cfg("data.http_timeout.tencent"),
                    impersonate="chrome131"
                )
                if r.status_code != 200:
                    continue
                data = r.json().get("data", {})
                klines = data.get("klines")
                if not klines:
                    continue
                for k_str in klines:
                    p = k_str.split(",")
                    d = p[0]
                    if d < start_date:
                        continue
                    rows.append(self._norm_row(
                        sym, d,
                        float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                        float(p[5]),      # vol (手, eastmoney 直接就是手)
                        float(p[6]) / 1000 if len(p) > 6 and p[6] else 0.0,  # amt 元→千元
                       0.0))
            except Exception:
                logger.debug(f"[tencent/em82] {sym} request failed, skipping")
                continue
        if rows:
            logger.info(f"[tencent/em82] {len(symbols)} stocks: {len(rows)} rows")
        return rows

    def _fetch_akshare_daily(self, symbols: list, start_date: str) -> list:
        """akshare 逐只日线: vol=手, amt=元 →/1000→千元, 唯一有历史换手率✅

        TLS 指纹对抗: akshare 内部使用 requests 库, 被 eastmoney CDN JA3 检测拦截。
        临时替换 sys.modules['requests'] 为 curl_cffi.requests, 调用后恢复。
        来源: 2026-07-20 python requests → RemoteDisconnected, curl_cffi → HTTP 200。
        """
        import sys
        import requests as _orig_requests
        import curl_cffi.requests as _curl_requests

        _init_cache()
        _akshare_limiter.wait()
        try:
            import akshare as ak
        except ImportError:
            raise RuntimeError("akshare not installed")
        rows = []
        end_date = to_compact(datetime.today())  # akshare API只接受YYYYMMDD

        # Monkey-patch: 替换 requests 为 curl_cffi, 绕过 TLS 指纹检测
        sys.modules['requests'] = _curl_requests
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry(delay=3)
        def _fetch_one(sym, s, e):
            # delay=3: akshare(东方财富)默认1s偏激进, 3s给服务器冷却窗口
            return ak.stock_zh_a_hist(symbol=sym, period="daily",
                                      start_date=s, end_date=e, adjust="qfq")

        try:
            for sym in symbols:
                try:
                    df = _fetch_one(sym, to_compact(start_date), end_date)
                except Exception as _e:
                    _retry_tries, _retry_delay = 4, 3
                    logger.warning(f"[akshare] {sym} retry exhausted ({_retry_tries} attempts, delay={_retry_delay}s): {type(_e).__name__}: {_e}")
                    continue
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append(self._norm_row(
                        str(row["股票代码"]),
                        str(row["日期"]),  # _norm_row → to_str() 自动归一化
                        float(row.get("开盘", 0) or 0), float(row.get("最高", 0) or 0),
                        float(row.get("最低", 0) or 0), float(row.get("收盘", 0) or 0),
                        float(row.get("成交量", 0) or 0),          # 手 ✅
                        float(row.get("成交额", 0) or 0) / 1000,   # 元→千元
                        float(row.get("换手率", 0) or 0)))
                import time; time.sleep(_require_cfg("data.rate_limit.akshare_per_stock_sec"))
        finally:
            sys.modules['requests'] = _orig_requests

        if rows:
            logger.info(f"[akshare] {len(symbols)} stocks: {len(rows)} rows (vol=手✅, amt/1000→千元)")
        return rows

    def _fetch_zzshare_daily(self, symbols: list, start_date: str) -> list:
        """zzshare 逐只日线: vol=手, amt=千元 ✅ 无需换算"""
        try:
            from zzshare.client import DataApi
            api = DataApi()
        except ImportError:
            raise RuntimeError("zzshare not installed")
        rows = []
        end_date = to_compact(datetime.today())  # akshare API只接受YYYYMMDD
        for sym in symbols:
            ts_code = _ts_code(sym)
            df = api.daily(ts_code=ts_code, start_date=to_compact(start_date), end_date=end_date)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                rows.append(self._norm_row(
                    sym, str(row["trade_date"])[:10],  # _norm_row → to_str() 归一化
                    float(row.get("open", 0) or 0), float(row.get("high", 0) or 0),
                    float(row.get("low", 0) or 0), float(row.get("close", 0) or 0),
                    float(row.get("vol", 0) or 0), float(row.get("amount", 0) or 0), 0.0))
        if rows:
            logger.info(f"[zzshare] {len(symbols)} stocks: {len(rows)} rows (vol=手, amt=千元)")
        return rows

    def _fetch_tickflow_daily(self, symbols: list, start_date: str = None) -> list:
        """TickFlow 批量日线: vol=手✅, amt=元❌→/1000→千元。

        历史K线先试注册版 TickFlow(api_key), 无批量K权限/未配置 → 免费层
        (test-v303 权限感知故障转移, 显式日志),
        当天数据用 API key _fetch_tickflow_quotes() 补充。
        来源: tickflow 免费版 "日K为历史数据, 盘中不会实时更新";
              API key 支持 tf.quotes.get() 实时行情含 turnover_rate
        """
        # 当天: 直接用 API key, 跳过免费版 (免费版日K不含当天)
        _tdy = datetime.today().strftime("%Y-%m-%d")
        # to_compact 归一化: 防止 YYYYMMDD vs YYYY-MM-DD 格式不匹配
        if start_date and to_compact(start_date) >= to_compact(_tdy):
            return self._fetch_tickflow_quotes(symbols, start_date)
        try:
            from tickflow import TickFlow
        except ImportError:
            raise RuntimeError("tickflow not installed (pip install tickflow)")
        rows = []
        def _tickflow_code(s):
            if s.startswith('920'): return f"{s}.BJ"       # BSE 北京交易所
            if s.startswith(('6','9','68')): return f"{s}.SH"  # 上海
            return f"{s}.SZ"                               # 深圳
        codes = [_tickflow_code(s) for s in symbols]
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _call_tickflow_batch(client, codes):
            return client.klines.batch(codes, period="1d", count=10000, as_dataframe=True, show_progress=False)

        # test-v303: 权限感知故障转移 — 先试注册版批量K (api.tickflow.org),
        # PermissionError (套餐无批量K权限, 2026-07-26 实测) → 记 flag 落免费层;
        # 升级套餐后新进程自动走回注册版。注册端单次尝试不过 retry:
        # 权限错误重试 4 次 × 15s 纯属浪费。
        global _TICKFLOW_BATCH_NO_PERM
        dfs = None
        if not _TICKFLOW_BATCH_NO_PERM:
            try:
                _api_key = _require_cfg("data.tickflow_api_key")
            except KeyError:
                _api_key = None
            if _api_key:
                try:
                    dfs = TickFlow(api_key=_api_key).klines.batch(
                        codes, period="1d", count=10000, as_dataframe=True, show_progress=False)
                    logger.info(f"[tickflow] 注册版批量K线 OK ({len(codes)} codes)")
                except Exception as _e:
                    from tickflow import PermissionError as _TFPermissionError
                    if isinstance(_e, _TFPermissionError):
                        _TICKFLOW_BATCH_NO_PERM = True
                    logger.warning(
                        f"[tickflow] 注册版批量K线失败 ({type(_e).__name__}: {_e}) → 免费层")
            else:
                logger.info("[tickflow] data.tickflow_api_key 未配置 → 免费层 (仅历史日K)")
        if dfs is None:
            dfs = _call_tickflow_batch(TickFlow.free(), codes)
        # B-08: tickflow 日K 未复权 → 本地 adj_factor 表转 qfq (同 tushare 口径,
        # test-v304), 不再直接落库混入 qfq 表 — 除权日收益率跳变, 回测不可复现。
        # 无本地因子覆盖的股票跳过 (不写口径不一致数据); 全缺 → None 交下一源。
        _conn = self._connect()
        _latest_map, _factor_map = self._local_qfq_ratio(_conn, symbols)
        _covered = set(_latest_map)
        if not _covered:
            logger.warning("[tickflow] no local adj_factor coverage — run "
                           "sync_adj_factor first; skip raw write, next source")
            return None
        dfs = {c: d for c, d in dfs.items() if c.split(".")[0] in _covered}
        if not dfs:
            return None
        for code, df in dfs.items():
            if df.empty:
                continue
            sym = code.split(".")[0]
            # ratio = factor(date) / latest_factor; 停牌日无因子记录 → 该股内 ffill/bfill,
            # 仍缺失则当天不复权 (ratio=1)。df 先按日期排序保证填充方向正确。
            df = df.sort_values("trade_date")
            _fmap = _factor_map.get(sym, {})
            _tds = df["trade_date"].astype(str).str[:10]
            # 归一化 YYYYMMDD → YYYY-MM-DD (与 adj_factor 表键一致)
            _td_iso = _tds.where(
                _tds.str.contains("-"),
                _tds.str[:4] + "-" + _tds.str[4:6] + "-" + _tds.str[6:8])
            df["adj_factor"] = [_fmap.get(d) for d in _td_iso]
            df["adj_factor"] = df["adj_factor"].ffill().bfill()
            # 全 None (K线日期与因子日期零重叠) → to_numeric 转 NaN, 防 object/除法 TypeError
            df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
            _ratio = (df["adj_factor"] / _latest_map[sym]).fillna(1.0)
            for _col in ("open", "high", "low", "close"):
                df[_col] = (df[_col].astype(float) * _ratio).round(4)
            for _, row in df.iterrows():
                d = str(row.get("trade_date", ""))[:10]  # _norm_row → to_str() 归一化
                if len(d) < 8:  # 至少8位才算有效日期
                    continue
                rows.append(self._norm_row(
                    sym, d,
                    float(row.get("open", 0) or 0), float(row.get("high", 0) or 0),
                    float(row.get("low", 0) or 0), float(row.get("close", 0) or 0),
                    float(row.get("volume", 0) or 0),          # 手 ✅
                    float(row.get("amount", 0) or 0) / 1000,   # 元→千元
                    0.0))
        # 当天数据用 API key 补充 (免费版日K不含当天)
        from datetime import datetime as _dt
        _td = _dt.today().strftime('%Y-%m-%d')
        _qd = [r[1] for r in rows]
        if _td not in _qd:
            _qr = self._fetch_tickflow_quotes(symbols, _td)
            if _qr:
                rows.extend(_qr)
                logger.info(f'[tickflow] +{len(_qr)} today rows from API key quotes')
        if rows:
            logger.info(f"[tickflow] {len(symbols)} stocks: {len(rows)} rows "
                        f"(qfq via local factors, {len(_covered)}/{len(symbols)} covered; "
                        f"vol=手✅, amt/1000→千元)")
        return rows

    def _fetch_tickflow_quotes(self, symbols: list, date: str) -> list:
        """TickFlow API key 实时行情 → 日线行格式, 含 turnover_rate。

        当天数据源 — TickFlow.free().klines.batch() 不含当天K线
        ("日K数据为历史数据, 盘中不会实时更新")。
        注册 API key 支持 tf.quotes.get() 实时行情, 含 turnover_rate。
        来源: tickflow.org 注册文档; config data.tickflow_api_key
        """
        from tickflow import TickFlow
        _api_key = _require_cfg("data.tickflow_api_key")
        tf = TickFlow(api_key=_api_key)

        def _tickflow_code(s):
            # 幂等: 已带后缀不再加
            if '.' in s: return s
            if s.startswith('920'): return f"{s}.BJ"
            if s.startswith(('6','9','68')): return f"{s}.SH"
            return f"{s}.SZ"

        codes = [_tickflow_code(s) for s in symbols]

        # tickflow quotes API 单次最大 5 只, 超过分块
        _batch_max = 5
        rows = []
        for _i in range(0, len(codes), _batch_max):
            _chunk = codes[_i:_i + _batch_max]
            from quant.data.datasource_retry import datasource_retry

            @datasource_retry
            def _call_tickflow_quotes(chunk):
                return tf.quotes.get(symbols=chunk, as_dataframe=True)

            try:
                quotes_df = _call_tickflow_quotes(_chunk)
            except Exception as _e:
                logger.warning(f"[tickflow quotes] chunk {_i} retry exhausted (4 attempts, 1-2-4-8s): {_e}")
                continue
            if quotes_df is None or quotes_df.empty:
                continue
            for _, q in quotes_df.iterrows():
                sym = str(q.get("symbol", "")).split(".")[0]
                if not sym:
                    continue
                _turnover = float(q.get("ext.turnover_rate", 0) or 0)

                rows.append(self._norm_row(
                    sym, date,
                    float(q.get("open", 0) or 0),
                    float(q.get("high", 0) or 0),
                    float(q.get("low", 0) or 0),
                    float(q.get("last_price", 0) or 0),
                    float(q.get("volume", 0) or 0),
                    float(q.get("amount", 0) or 0) / 1000,
                    _turnover))

        if rows:
            logger.info(f"[tickflow quotes] {len(symbols)} stocks: {len(rows)} rows (vol+turnover✅)")
        return rows


    def _fetch_longbridge_daily(self, symbols: list, start_date: str = None) -> list:
        """Longbridge (longport) 日线 — 前复权, vol=股✅, amt=元✅。

        需要: pip install longport + 配置 LONGPORT_APP_KEY/LONGPORT_APP_SECRET/LONGPORT_ACCESS_TOKEN
        未安装或未配置 → 静默回退下一源。
        免费额度: 日K线 100次/分钟, 每次最多 200 只股票。
        """
        try:
            import longport as _lb
        except ImportError:
            logger.info("[longbridge] longport not installed, skip — pip install longport")
            return []

        app_key = os.environ.get("LONGPORT_APP_KEY")
        app_secret = os.environ.get("LONGPORT_APP_SECRET")
        access_token = os.environ.get("LONGPORT_ACCESS_TOKEN")
        if not all([app_key, app_secret, access_token]):
            logger.info("[longbridge] missing credentials (LONGPORT_APP_KEY/SECRET/TOKEN), skip")
            return []

        try:
            config = _lb.Config(
                app_key=app_key, app_secret=app_secret, access_token=access_token
            )
            ctx = _lb.QuoteContext(config)
        except Exception as e:
            logger.warning(f"[longbridge] connection failed: {e}, skip")
            return []

        rows = []
        try:
            for sym in symbols:
                try:
                    # A股 → longport 格式: 000001.SZ → 000001.SZ
                    resp = ctx.history_candlesticks_by_offset(
                        sym, _lb.Period.Day, _lb.AdjustType.Forward,
                        count=1, end_date=datetime.now()
                    )
                    if resp and len(resp) > 0:
                        c = resp[0]
                        rows.append({
                            "symbol": sym.replace(".SZ", "").replace(".SH", ""),
                            "date": c.timestamp.strftime("%Y-%m-%d"),
                            "open": float(c.open), "high": float(c.high),
                            "low": float(c.low), "close": float(c.close),
                            "volume": int(c.volume), "amount": float(c.volume * (c.high + c.low + c.close) / 3) if c.amount == 0 else float(c.amount),
                            "turnover": None,
                        })
                except Exception:
                    continue
        finally:
            try:
                ctx.close() if hasattr(ctx, 'close') else None
            except Exception as _e:
                logger.debug("db context close failed (non-fatal): %s", _e)

        logger.info(f"[longbridge] {len(rows)} rows for {len(symbols)} symbols")
        return rows

    def _fetch_pytdx_daily(self, symbols: list, start_date: str) -> list:
        """Pytdx (通达信) 日线 + 前复权: vol=手, amt=元→/1000→千元。

        数据源: 通达信 (Tong Da Xin) 标准行情协议 — 国内最老牌的免费行情协议。
        提供商: 财富趋势科技 (已上市, 股票代码 688318), 通达信客户端覆盖绝大多数券商。
        服务器: 180.153.18.170:7709 (TCP 直连, 无需 API key, 无需认证).
        特点: 数据质量可靠、稳定运行 30 年+、无频率限制 (TCP 逐只拉取).
        缺点: 不提供换手率 (turnover=0 回填)、未复权需手算前复权因子、逐只拉取慢.
        Pytdx 返回未复权数据，通过 get_xdxr_info 获取除权除息记录手算前复权因子。

        来源: ③ Pytdx 是国内最老牌的免费行情协议，数据质量可靠。
        """
        try:
            from pytdx.hq import TdxHq_API
        except ImportError:
            raise RuntimeError("pytdx not installed")

        api = TdxHq_API()
        # socket pre-probe: avoid C extension connect() blocking indefinitely
        import socket as _socket
        _connect_timeout = _require_cfg("data.pytdx.connect_timeout")
        _sock = _socket.create_connection(("180.153.18.170", 7709), timeout=_connect_timeout)
        _sock.close()
        if not api.connect('180.153.18.170', 7709):
            logger.warning("pytdx: server unreachable")
            api.disconnect()
        rows = []
        try:
            for sym in symbols:
                # 市场: 0=深圳, 1=上海
                if sym.startswith(('0', '2', '3')):
                    market = 0
                else:
                    market = 1

                # 1. 获取除权除息记录 (用于前复权计算)
                xdxr = api.get_xdxr_info(market, sym)
                adj_map = {}
                if xdxr:
                    events = []
                    for r in xdxr:
                        songzhuan = float(r.get('songzhuangu', 0) or 0)
                        if songzhuan > 0:
                            d = '%d-%02d-%02d' % (r['year'], r['month'], r['day'])
                            events.append((d, 1 + songzhuan / 10))
                    if events:
                        events.sort(key=lambda x: x[0])
                        # cum[i] = product of (1+R) from events[0] to events[i]
                        cum = 1.0
                        for d, ratio in events:
                            cum *= ratio
                            adj_map[d] = cum
                        # Now for a bar date D, factor = 1 / product of events AFTER D
                        # = 1 / (cum_last / cum_at_or_before_D)
                        # Actually simpler: for each bar date, multiply by 1/ratio for each event after it

                # 3. 获取日线
                bars = api.get_security_bars(9, market, sym, 0, 2000)
                if not bars:
                    continue

                # 对每个bar应用前复权
                for b in bars:
                    d = '%d-%02d-%02d' % (b['year'], b['month'], b['day'])
                    if d < start_date:
                        continue

                    o, h, l, c = (float(b['open']), float(b['high']),
                                  float(b['low']), float(b['close']))
                    vol = float(b['vol'])
                    amt = float(b['amount'])

                    # 前复权: 找到日期 >= d 的除权事件，累积复权因子
                    # factor = 1 / product(ratio for event_date > d)
                    factor = 1.0
                    if adj_map:
                        # cum_at_date = product of ratios up to and including d
                        # We need 1 / product of ratios AFTER d
                        cum_before = 1.0
                        cum_all = 1.0
                        found = False
                        for ed, ratio in sorted(adj_map.items()):
                            cum_all = ratio
                            if ed <= d:
                                cum_before = ratio
                                found = True
                        # ratios after d = cum_all / cum_before (if cum_before != 0)
                        # factor for prices at d = 1 / (ratios after d)
                        if found and cum_before > 0:
                            factor = cum_before / cum_all
                        else:
                            factor = 1.0 / cum_all

                    o_adj = round(o * factor, 4)
                    h_adj = round(h * factor, 4)
                    l_adj = round(l * factor, 4)
                    c_adj = round(c * factor, 4)
                    # vol in 手, amt in 元→千元, turnover=0 (pytdx 不提供换手率)
                    rows.append(self._norm_row(sym, d, o_adj, h_adj, l_adj, c_adj, vol, amt / 1000, 0.0))

        finally:
            api.disconnect()

        if rows:
            logger.info(f"[pytdx] {len(symbols)} stocks: {len(rows)} rows (vol=手, amt/1000→千元, qfq manual adj)")
        return rows

    def backfill_range(self, start: str, end: str, symbols: list = None):
        """按日期范围精准回补缺失的日线 (test-v349).

        与 update_daily() 不同: 直接查缺口, 不依赖 gap 分析的 MAX(date) 逻辑.
        发现缺数据后一次调用 update_daily(symbols=missing, start=..., target_date=...)
        利用已有的 RateLimiter + 多源回退链完成拉取.

        返回: 新写入的行数.
        """
        conn = self._connect()
        if symbols is None:
            symbols = [r[0] for r in conn.execute(
                "SELECT symbol FROM stocks WHERE market!='BJ'").fetchall()]
        all_symbols = set(symbols)

        # 查找区间内完全没有数据的股票
        have_any = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM daily WHERE date>=? AND date<=?",
            (start, end)).fetchall()}
        missing = sorted(all_symbols - have_any)
        # 不关闭 conn — update_daily 复用同一线程连接

        if not missing:
            logger.info(f"backfill_range: {start}→{end} — all {len(all_symbols)} stocks OK")
            return 0

        logger.info(f"backfill_range: {start}→{end} — "
                    f"{len(missing)}/{len(all_symbols)} stocks missing, pulling via update_daily")
        return self.update_daily(symbols=missing, start=start, target_date=end, _explicit_start=True)
        """回填换手率 — baostock 逐只拉取 K 线, 取 turn 字段 UPDATE daily。

        date: 指定时只回填该日; None 时扫描全缺口 (历史存量回填).
        baostock 免费无需注册, 无硬限速, 建议 0.3s/只间隔 (来源: 2026-07-21 实测)。
        turn 值与 tushare daily_basic turnover_rate 完全一致 (600519: 0.8492%)。
        来源: scripts/check_turnover_sources.py 实测 + baostock 官方文档。
        """
        import time as _time
        from datetime import datetime, timedelta
        from quant.execution.calendar import is_trading_day
        conn = self._connect()

        # ── 确定回填日期范围 ──
        if date:
            # 单日模式: 只回填指定日期, 跳过全缺口扫描
            needs_fill = {}
            syms = [r[0] for r in conn.execute(
                "SELECT symbol FROM daily WHERE date=? AND (turnover=0 OR turnover IS NULL)", (date,)
            ).fetchall()]
            if syms:
                needs_fill[date] = syms
            if not needs_fill:
                logger.info(f"turnover backfill {date}: all stocks have turnover, nothing to do")
                return 0
            total_stocks = len(syms)
            gap_dates = [date]
            gap_start_dt = datetime.strptime(date, "%Y-%m-%d")
            gap_end_dt = gap_start_dt
        else:
            # 全量模式: 扫描所有缺口日期
            last_good = conn.execute("SELECT MAX(date) FROM daily WHERE turnover>0").fetchone()[0]
            if last_good is None:
                logger.info("turnover backfill: no turnover>0 data")
                return 0

            _today = datetime.today()
            gap_start_dt = datetime.strptime(last_good, "%Y-%m-%d")
            gap_end_dt = _today

            gap_dates = []
            for d_offset in range((gap_end_dt - gap_start_dt).days + 1):
                d = (gap_start_dt + timedelta(days=d_offset)).strftime("%Y-%m-%d")
                if is_trading_day(datetime.strptime(d, "%Y-%m-%d").date()):
                    gap_dates.append(d)

            if not gap_dates:
                logger.info("turnover backfill: no trading days in gap, nothing to do")
                return 0

            # ── 收集需回填的 (symbol, date) ──
            needs_fill = {}
            for d in gap_dates:
                syms = [r[0] for r in conn.execute(
                    "SELECT symbol FROM daily WHERE date=? AND (turnover=0 OR turnover IS NULL)", (d,)
                ).fetchall()]
                if syms:
                    needs_fill[d] = syms

            if not needs_fill:
                logger.info("turnover backfill: all dates have complete turnover, nothing to do")
                return 0

            total_stocks = sum(len(v) for v in needs_fill.values())

        _est_sec = total_stocks * 0.3 + total_stocks * 0.15
        logger.info(f"turnover backfill: {total_stocks} stock×dates via baostock, ~{_est_sec/60:.0f}min estimated")

        # ── baostock login ──
        import baostock as _bs
        try:
            _lg = _bs.login()
            if _lg.error_code != '0':
                logger.error(f"baostock login failed: {_lg.error_msg}")
                return 0
            logger.info(f"baostock login: {_lg.error_msg}")
        except Exception as _e:
            logger.error(f"baostock import/login failed: {_e}")
            return 0

        # ── baostock K线拉取间隔 ──
        _BS_INTERVAL = _require_cfg("data.rate_limit.baostock_per_stock_sec")  # 来源: config.yaml, 默认0.3s

        _bs_t0 = _time.time()
        total_updated = 0
        _bs_processed = 0
        logger.info(f"turnover backfill: starting, first progress at 50 stocks (~{_BS_INTERVAL * 50 + 7.5:.0f}s)")
        for d_offset in range((gap_end_dt - gap_start_dt).days + 1):
            d = (gap_start_dt + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            if not is_trading_day(datetime.strptime(d, "%Y-%m-%d").date()):
                continue
            syms = needs_fill.get(d, [])
            if not syms:
                continue

            logger.info(f"turnover backfill {d}: {len(syms)} stocks via baostock")
            updated_today = 0
            for i, sym in enumerate(syms):
                code = _bs_code(sym)
                # ── baostock 查询, 3次重试 ──
                tv = 0.0
                for _retry in range(3):
                    try:
                        _rs = _bs.query_history_k_data_plus(
                            code, "date,turn",
                            start_date=d, end_date=d,
                            frequency="d", adjustflag="2")
                        if _rs.error_code == '0':
                            while _rs.next():
                                row = _rs.get_row_data()
                                if row[0] == d:
                                    tv_str = row[1] if len(row) > 1 else ''
                                    tv = float(tv_str) if tv_str and tv_str.strip() else 0.0
                                    break
                        else:
                            if _retry == 0 and _bs_processed < 5:
                                logger.warning(f"turnover backfill {d}: baostock {code} error — {_rs.error_msg}")
                        break  # 跳出重试循环
                    except Exception as _e:
                        if _retry < 2:
                            _time.sleep(2 * (_retry + 1))  # 退避: 2s/4s/6s
                        else:
                            logger.warning(f"turnover backfill {d}: baostock {code} failed after 3 retries — {_e}")

                if tv > 0:
                    conn.execute("UPDATE daily SET turnover=? WHERE symbol=? AND date=?", (tv, sym, d))
                    updated_today += 1

                _bs_processed += 1
                # 每 5000 只重登, 防止 session 超时导致 Broken pipe (baostock 免费服务 ~1-2h 超时)
                if _bs_processed > 0 and _bs_processed % 200 == 0:
                    logger.info(f"turnover backfill: baostock re-login at {_bs_processed} stocks")
                    _bs.logout()
                    _lg = _bs.login()
                    if _lg.error_code != '0':
                        logger.warning(f"baostock re-login failed: {_lg.error_msg}")
                if _bs_processed % 50 == 0:
                    _elapsed = _time.time() - _bs_t0
                    _rate = _bs_processed / _elapsed if _elapsed > 0 else 0
                    _eta = (total_stocks - _bs_processed) / _rate if _rate > 0 else 0
                    logger.info(f"turnover backfill: {_bs_processed}/{total_stocks} ({100*_bs_processed//total_stocks}%) "
                                f"{_rate:.1f}stocks/s ETA={_eta/60:.0f}min today={updated_today} total={total_updated}")
                if _bs_processed % 100 == 0:
                    conn.commit()  # 每100只提交一次, 防数据丢失
                _time.sleep(_BS_INTERVAL)

            conn.commit()
            logger.info(f"turnover backfill {d}: done — {updated_today}/{len(syms)} updated")
            total_updated += updated_today

        # ── baostock logout ──
        _bs.logout()
        logger.info("baostock logout")

        conn.commit()
        logger.info(f"turnover backfill: {total_updated}/{total_stocks} stocks updated total")
        return total_updated

    def backfill_turnover_quotes(self, date: str = None):
        """用 tickflow 实时行情回填当日换手率。

        免费注册版不支持 universes 查询, 改为从 stocks 表取全量 symbol,
        转 tickflow 格式后调用 quotes.get(symbols=...)。
        每批 500 只, 避免超长 URL。
        """
        try:
            from tickflow import TickFlow
        except ImportError:
            logger.warning("tickflow not installed")
            return 0
        tf_key = _require_cfg("data.tickflow_api_key")
        if not tf_key:
            logger.warning("tickflow api key not configured")
            return 0

        from datetime import datetime
        if date is None:
            _tmp_conn = self._connect()
            row = _tmp_conn.execute("SELECT MAX(date) FROM daily WHERE volume>0").fetchone()
            date = row[0] if row and row[0] else datetime.today().strftime("%Y-%m-%d")

        tf = TickFlow(api_key=tf_key)
        conn = self._connect()
        # 只取该日期有 daily 数据且 turnover 为 0/NULL 的股票
        all_syms = [r[0] for r in conn.execute(
            "SELECT symbol FROM daily WHERE date=? AND (turnover=0 OR turnover IS NULL)", (date,)
        ).fetchall()]
        if not all_syms:
            logger.info(f"turnover backfill: no stocks need turnover for {date}")
            return 0

        def _to_tf(sym):
            if sym.startswith(("6", "9", "68")): return sym + ".SH"
            if sym.startswith(("4", "8", "92")): return sym + ".BJ"
            return sym + ".SZ"

        batch_size = 5
        total_updated = 0
        batch_count = (len(all_syms) + batch_size - 1) // batch_size
        logger.info(f"turnover backfill: {len(all_syms)} stocks, {batch_count} batches, ~{batch_count*6/60:.0f}min estimated")
        _progress_interval = max(50, len(all_syms) // 20)  # 至少每50只打印一次, 最多20次进度

        t_start = __import__('time').time()
        for batch_idx, i in enumerate(range(0, len(all_syms), batch_size)):
            chunk = all_syms[i:i + batch_size]
            tf_symbols = [_to_tf(s) for s in chunk]
            try:
                quotes = tf.quotes.get(symbols=tf_symbols, as_dataframe=True)
            except Exception as e:
                logger.warning(f"tickflow quotes chunk {i}: {e}")
                continue
            if quotes is None or quotes.empty:
                continue
            turnover_col = None
            for col in quotes.columns:
                if "turnover" in str(col).lower():
                    turnover_col = col
                    break
            if turnover_col is None:
                continue
            for _, row in quotes.iterrows():
                sym = str(row["symbol"]).split(".")[0]
                tv = row.get(turnover_col, 0)
                tv = float(tv) if tv and tv == tv else 0.0
                if tv > 0:
                    conn.execute(
                        "UPDATE daily SET turnover=? WHERE symbol=? AND date=? AND (turnover=0 OR turnover IS NULL)",
                        (tv, sym, date))
                    total_updated += 1
            conn.commit()
            import time; time.sleep(_require_cfg("data.rate_limit.tickflow_quote_batch_sec"))  # tickflow 免费版 10次/分钟; 来源: test-v169 实测
            _stocks_done = i + len(chunk)
            # 动态进度: 每 50 只或进度达 5% 阶梯打印, 避免长时间无输出
            if _stocks_done % _progress_interval == 0 or batch_idx % 10 == 0 or batch_idx == batch_count - 1:
                elapsed = __import__("time").time() - t_start
                _rate = _stocks_done / max(elapsed, 0.001)
                _remaining = len(all_syms) - _stocks_done
                _eta = _remaining / max(_rate, 0.001)
                logger.info(f"turnover backfill: {_stocks_done}/{len(all_syms)} "
                           f"({100*_stocks_done//len(all_syms)}%) "
                           f"{_rate:.1f}stocks/s ETA={_eta:.0f}s today={total_updated}")
        logger.info(f"turnover backfill (tickflow): {total_updated} stocks for {date}")
        return total_updated
    def _sync_industry_akshare(self, conn) -> int:
        """akshare 逐只查询行业回退 — 仅针对 industry IS NULL 的股票。

        stock_board_industry_cons_ths() 批量API不稳定，改用 stock_individual_info_em()
        逐只查询行业，只对未分类的317只股票。
        每只 ~1秒，总共 ~5分钟。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not installed — industry sync skipped")
            return 0
        missing = [r[0] for r in conn.execute(
            "SELECT symbol FROM stocks WHERE industry IS NULL"
        ).fetchall()]
        if not missing:
            logger.info("industry sync: no unclassified stocks")
            return 0
        logger.info(f"industry sync: {len(missing)} unclassified stocks via akshare individual")
        import time
        from quant.data.datasource_retry import datasource_retry
        updated = 0
        for idx, sym in enumerate(missing):
            @datasource_retry
            def _fetch_industry(sym=sym):
                return ak.stock_individual_info_em(symbol=sym)

            info = _fetch_industry()
            if info is None or info.empty:
                continue
            # stock_individual_info_em 返回 行×列 格式, industry在'值'列中
            info_dict = dict(zip(info['item'], info['value']))
            industry = str(info_dict.get('行业', info_dict.get('industry', ''))).strip()
            if industry:
                conn.execute(
                    "UPDATE stocks SET industry=? WHERE symbol=?",
                    (industry, sym)
                )
                updated += 1
            if idx < 3:
                logger.info(f"stock {sym}: industry='{industry}', items={list(info_dict.keys())[:5]}")
            time.sleep(_require_cfg("data.rate_limit.akshare_industry_sec"))  # akshare rate limit
        conn.commit()
        classified = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        logger.info(f"industry sync (akshare individual): {updated} updates, {classified}/{total}")
        return updated

    def _analyze_daily_gaps(self, conn, target_date: str = None) -> dict:
        """分析日线数据缺口 — missing（从未有数据） vs stale（超250天未更新） vs stale_recent（近期缺数据） vs full（完整）。

        用于增量更新前的精准拉取决策, 只拉缺口 + 过期数据, 不浪费 API 配额。

        target_date: 指定时以此为 staleness 基准 (替代今天), 已有该日数据的股票 → full.
                    用于补历史单日数据时避免全市场重拉.

        两级检查:
          1. 长期过期: max(date) < stale_days 天前 → stale
          2. 近期缺失: 最新 DB 日期落后于参考日期 → 所有有数据的股票判为 stale_recent
             (例如 DB 最新 07-13 但参考日期 07-16, 则全部股票需补拉 07-14/15/16)
        """
        from datetime import datetime, timedelta
        stale_days = _require_cfg("data.stale_days")  # 数据过期阈值
        cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")

        # 单次查询: PK(symbol,date) 覆盖索引, GROUP BY symbol 只取首尾
        rows = conn.execute("""
            SELECT symbol, MIN(date), MAX(date)
            FROM daily GROUP BY symbol ORDER BY symbol
        """).fetchall()

        # global latest date — 用于近期缺失检测
        global_max = conn.execute("SELECT MAX(date) FROM daily WHERE date >= '2000-01-01' AND date < '2100-01-01'").fetchone()[0] or "2020-01-01"

        # 最近交易日 — target_date 指定时以此为基准, 否则取今天
        today_str = datetime.now().strftime("%Y-%m-%d")
        ref_date = target_date if target_date else today_str
        try:
            from quant.execution.calendar import get_trading_days
            all_td = sorted(get_trading_days())
            most_recent_td = [d for d in all_td if d <= ref_date][-1] if all_td else ref_date
        except Exception:
            most_recent_td = ref_date

        # 所有 stocks 符号
        all_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market!=\"BJ\"").fetchall()}
        have_data = set()

        # 每个 symbol 单独判断是否缺最新交易日数据
        stale, stale_recent, full = [], [], []
        for sym, min_d, max_d in rows:
            have_data.add(sym)
            # 只处理 stocks 表内的股票; ETF/退市股等在 daily 有历史残留但不应拉取
            if sym not in all_symbols:
                continue
            if max_d < cutoff:
                stale.append(sym)
                continue
            # 逐股检查是否缺最新交易日数据 (而非全局 recent_stale)
            if max_d < most_recent_td:
                stale_recent.append(sym)
                continue
            full.append(sym)

        missing = sorted(all_symbols - have_data)

        return {
            "missing": missing, "stale": stale, "stale_recent": stale_recent, "full": full,
            "total": len(all_symbols),
        }


    def update_daily(self, symbols: list = None,
                     start: str = None,
                     target_date: str = None,
                     _explicit_start: bool = False) -> int:
        """增量更新日线 — 精准缺口分析 + 多源回退。

        target_date: 指定时只拉该日缺口 (替代全量 staleness 检查).
                     用于补历史单日数据, 避免已有该日数据的股票重入拉取链.

        流程:
          1. 分析哪些股票缺少数据（不浪费时间拉已有数据）
          2. tushare(批量50股,qfq) → zzshare → pytdx(通达信TCP) → baostock(证券宝) → tencent → akshare → tickflow → longbridge
          3. OHLCV 完成后，Baostock 补充换手率

        symbols: None 表示自动分析缺口并只拉缺失/不足的股票
        返回: 新写入的行数
        """
        if start is None:
            start = _require_cfg("data.start_date")

        conn = self._connect()

        # 1. 精准分析数据缺口
        if symbols is None:
            gaps = self._analyze_daily_gaps(conn, target_date)
            target = gaps["missing"] + gaps["stale"] + gaps.get("stale_recent", [])
            logger.info(f"daily gaps: {gaps['total']} total, "
                       f"{len(gaps['missing'])} missing, "
                       f"{len(gaps['stale'])} stale(<250d), "
                       f"{len(gaps.get('stale_recent', []))} stale_recent, "
                       f"{len(gaps['full'])} full — pulling {len(target)}")
            if not target:
                logger.info("daily data complete, nothing to pull")
                return 0
            symbols = sorted(target, key=lambda s: s[:2])  # SH first (tushare benefit)

            # 当天快速路由: target_date 未指定 + 只有 stale_recent(无 missing/stale),
            # 覆写 start 为今天 → 跳过免费版(免费版日K不含当天, 来源: 2026-07-21 全链路逻辑分析).
            # target_date 指定时不走此路由: start=today 会拉错日期 (target_date≠today).
            if (target_date is None and gaps.get("stale_recent")
                    and not gaps["missing"] and not gaps["stale"]):
                start = datetime.today().strftime("%Y-%m-%d")
        else:
            logger.info(f"daily update: {len(symbols)} specified stocks, range={start}→{target_date or 'today'}")

        # 2. tushare 作为首选源 (self.token 从 __init__ 三阶回退读取)
        # _fetch_batch_tushare 内部自行创建 ts.pro_api(), 此处仅做 gate 判断
        # 来源: 2026-07-21 消除冗余 pro_api() 创建
        total_new = 0
        batch_size = _require_cfg("data.batch_size")  # 批量大小
        sources = {}     # source → count
        _t_loop = __import__('time').time()  # 进度日志用, 计算 ETA

        # total_new: INSERT ... ON CONFLICT DO UPDATE 语义下统计的是"受影响行数"(INSERT+UPDATE)
        # 非传统"新增行数" — 对 stale_recent 全量刷新场景会等于全量行数
        # turnover 列受 CASE WHEN 保护: 新源 turnover=0 时保留旧值 (来源: 2026-07-21)
        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i:i + batch_size]
            # test-v348: 历史回填直接使用 start, 正常增量用 DB MAX(date)
            if _explicit_start:
                batch_start = start
            else:
                batch_maxes = conn.execute(
                    f"SELECT symbol, MAX(date) FROM daily WHERE symbol IN ({','.join('?' for _ in chunk)}) GROUP BY symbol",
                    chunk
                ).fetchall()
                batch_start_map = {r[0]: r[1] for r in batch_maxes if r[1]}
                batch_start = (min(batch_start_map.values())
                              if batch_start_map else to_compact(start))
                if to_compact(batch_start) < to_compact(start):
                    batch_start = start

            rows = None
            source = "none"

            # 速度统计: 记录每源 rows/s 的 EMA 供监控排查 (仅记录, 不参与排序 —
            # 源顺序由下方 all_sources 固定优先级决定, 2026-07-26 审计纠偏)
            if not hasattr(self, '_source_speed'):
                self._source_speed = {}
            # B-08 fix: sina 从 all_sources 移除 — 返回未复权数据(除权日单日跳-34%),
            # 与本表 qfq 口径不一致, 混写导致收益率序列不可复现。
            # 各源复权口径: tushare=adj_factor 转 qfq (B-08), tickflow=adj_factor 转 qfq (B-08, test-v304),
            # zzshare/pytdx=前复权, tencent/akshare=em qfq 前复权。

            # ── 全量拉取源选择 (多源回退, 按优先级排序) ──
            # 各源简介:
            #   - tushare:     批量50股, qfq✅, turnover✅. 首家优选.
            #   - zzshare:     逐只拉取, 前复权, turnover=0.
            #   - pytdx:       通达信 (财富趋势 688318) — TCP 直连, 无需API key, 30年+稳定.
            #   - baostock:    证券宝 — 免费开源, qfq 前复权, turnover✅. 逐只 0.3s, 兜底可靠.
            #   - tencent:     EM K线, qfq 前复权, IP 当前封禁. 等解封后自动恢复.
            #   - akshare:     逐只拉取, EM qfq 前复权, turnover✅. IP 封禁中 → 置后减少白等.
            #   - tickflow:    批量拉取, adj_factor 转 qfq, 无 turnover. 免费版无批量权限.
            #   - longbridge:  批量拉取, 无 turnover. 需凭证.
            # akshare 排在 zzshare/pytdx/tencent 之后: IP 封禁期内减少无效重试 (4次×3s=12s/批).
            # 设计决策: 速度优先于 turnover 完整性 — tushare 首位的 99%+ 成功率保证了 turnover 覆盖率。
            # 若 tushare 某批失败, 回退源(无 turnover)接盘 → backfill_turnover 后续补 turnover。
            # TLS 指纹对抗: tencent/akshare 使用 curl_cffi 模拟 Chrome 131
            # 来源: 2026-07-20 scripts/test_all_sources_rate.py 全源实测; 2026-07-21 全链路逻辑分析
            all_sources = [
                ("zzshare", lambda: self._fetch_zzshare_daily(chunk, batch_start)),
                ("pytdx", lambda: self._fetch_pytdx_daily(chunk, batch_start)),
                ("baostock", lambda: self._fetch_baostock_daily(chunk, batch_start)),
                ("tencent", lambda: self._fetch_tencent_daily(chunk, batch_start)),
                ("akshare", lambda: self._fetch_akshare_daily(chunk, batch_start)),
                ("tickflow", lambda: self._fetch_tickflow_daily(chunk, batch_start)),
                ("longbridge", lambda: self._fetch_longbridge_daily(chunk, batch_start)),
            ]
            if self.token:
                all_sources.insert(0, ("tushare", lambda: self._fetch_batch_tushare(chunk, batch_start)))
            ordered = all_sources
            for src_name, fetch_fn in ordered:
                if rows is not None:
                    break
                t0 = __import__('time').time()
                try:
                    result = fetch_fn()
                except Exception as _src_err:
                    logger.warning(f"[{src_name}] fetch failed, trying next: {_src_err}")
                    self._source_speed[src_name] = 0  # deprioritize
                    continue
                elapsed = __import__('time').time() - t0
                if result:
                    rows = result
                    source = src_name
                    rps = len(result) / max(elapsed, 0.001)
                    # 指数移动平均: 70%旧+30%新, 防单次波动
                    old = self._source_speed.get(src_name, rps)
                    self._source_speed[src_name] = old * 0.7 + rps * 0.3
            if rows:
                # 入口校验: 过滤非 YYYY-MM-DD 格式的脏日期, 防止类似 '80846-51-5' 污染数据库
                import re as _re
                _date_ok = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
                _clean, _skipped = [], 0
                for _r in rows:
                    if _date_ok.match(str(_r[1])):
                        _clean.append(_r)
                    else:
                        _skipped += 1
                if _skipped:
                    logger.warning(f"daily [{source}] skipped {_skipped} rows with invalid date format")
                if not _clean:
                    rows = None
                else:
                    rows = _clean
                if rows:
                    conn.executemany(
                        """INSERT INTO daily
                       (symbol,date,open,high,low,close,volume,amount,turnover)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol, date) DO UPDATE SET
                       open=excluded.open, high=excluded.high, low=excluded.low,
                       close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                       turnover=CASE WHEN excluded.turnover > 0 THEN excluded.turnover ELSE turnover END""", rows
                    )
                    total_new += len(rows)
                    sources[source] = sources.get(source, 0) + 1

            # 每批打印进度 + 样本日志 (每批50只)
            conn.commit()
            # 取本批第一行做样本验证
            sample = rows[0] if rows else None
            sample_str = ""
            if sample:
                sample_str = f" | sample: {sample[0]} {sample[1]} V={sample[6]} Amt={sample[7]}"
            pct = min(i + batch_size, len(symbols)) / len(symbols) * 100
            done = min(i + batch_size, len(symbols))
            _elapsed = __import__("time").time() - _t_loop
            _remaining = len(symbols) - done
            _rate = done / max(_elapsed, 0.001)
            _eta = _remaining / max(_rate, 0.001)
            logger.info(f"daily [{source}] {done}/{len(symbols)} ({pct:.0f}%) {total_new}新行 | {_elapsed:.0f}s ETA={_eta:.0f}s{sample_str}")

            # tushare 限流由 _fetch_batch_tushare 内 RateLimiter 统一管控 (calls_per_minute 来自 config)
            # 不再额外 sleep — 避免双重限流 (来源: 2026-07-21 全链路逻辑分析)

        conn.commit()

        total_rows = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        src_summary = ", ".join(f"{k}:{v}" for k, v in sources.items() if v > 0) if sources else "none"
        logger.info(f"daily done: {total_rows} rows total ({total_new} new, sources: {src_summary})")
        return total_new

    # ============================================================
    # 读取数据
    # ============================================================

    def get_daily(self, symbols: list, start: str = DEFAULT_START_DATE,
                  end: str = None, columns: list = None) -> pd.DataFrame:
        """从 SQLite 读取日线，返回 (dates x stocks) 宽表 DataFrame。

        columns: 需要的列，默认全部。可只传 ['close','volume'] 节省 IO。
        自动分块避免 SQLite 的 999 参数上限。
        结果缓存: 同一次 DataStore 实例内相同参数只查一次 DB。"""
        # 来源: SQLite SQLITE_MAX_VARIABLE_NUMBER=999, 900+99(date params)=999
        MAX_SYMBOLS = 900
        # LRU cache: same (symbols, start, end, columns) -> reuse
        _ck = (tuple(sorted(symbols)[:200]), start, end, tuple(columns or []))
        _cached = self._query_cache.get(_ck)
        if _cached is not None:
            if columns:
                _have = [c for c in columns if c in _cached.columns.get_level_values(0)]
                if _have:
                    return _cached[_have].copy()
            return _cached.copy()
        if len(symbols) <= MAX_SYMBOLS:
            _result = self._get_daily_chunk(symbols, start, end, columns=columns)
            if len(self._query_cache) < 16:
                self._query_cache[_ck] = _result.copy()
            return _result

        frames = []
        for i in range(0, len(symbols), MAX_SYMBOLS):
            df = self._get_daily_chunk(symbols[i:i + MAX_SYMBOLS], start, end)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        # 按列合并（同一日期索引，不同股票列）
        result = frames[0]
        for df in frames[1:]:
            result = result.join(df, how='outer')
        return result

    def _get_daily_chunk(self, symbols: list, start: str = DEFAULT_START_DATE,
                          end: str = None, columns: list = None) -> pd.DataFrame:
        end = end or to_str(datetime.today())
        placeholders = ",".join("?" for _ in symbols)
        conn = self._connect()
        df = pd.read_sql_query(
            f"""SELECT symbol, date, open, high, low, close, volume, amount, turnover
                FROM daily
                WHERE symbol IN ({placeholders})
                  AND date >= ? AND date <= ?
                ORDER BY date""",
            conn, params=symbols + [start, end]
        )
        if df.empty:
            return pd.DataFrame({})
        df["date"] = pd.to_datetime(df["date"])
        if columns:
            return df.pivot(index="date", columns="symbol", values=columns).ffill()
        result = df.pivot(index="date", columns="symbol", values=[
            "open", "high", "low", "close", "volume", "amount", "turnover"
        ])
        return result.ffill()  # 停牌日填前一日价格，NaN 不进管线

    def get_stock_count(self) -> dict:
        conn = self._connect()
        n_stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        n_daily = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM daily WHERE date >= '2000-01-01' AND date < '2100-01-01'"
        ).fetchone()
        return {
            "stocks": n_stocks,
            "daily_rows": n_daily,
            "date_min": date_range[0],
            "date_max": date_range[1],
            "trading_days": date_range[2],
        }

    def rank_by_turnover(self, symbols: list, date: str, lookback_days: int = 60,
                         top_n: int = 800) -> list:
        """按日均成交额降序取 top N 股票。复用 DataStore 连接。"""
        conn = self._connect()
        t0 = (pd.Timestamp(date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ph = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"SELECT symbol, AVG(amount) as avg_amt FROM daily "
            f"WHERE date >= ? AND symbol IN ({ph}) "
            f"GROUP BY symbol ORDER BY avg_amt DESC LIMIT ?",
            [t0] + list(symbols) + [top_n]
        ).fetchall()
        return [r[0] for r in rows] if rows else list(symbols)[:top_n]

    def sync_fundamentals(self) -> int:
        """同步 PE/PB/市值 — 批量PE+市值, 逐只补PB, 多源容错"""
        try:
            from quant.data.fundamental import sync_all
            result = sync_all(self._connect(), max_fetch=-1)
            logger.info(f"fundamentals: PE={result['pe_count']} PB={result['pb_count']}")
            return result["pe_count"]
        except (ImportError, ModuleNotFoundError):
            logger.warning("fundamentals sync skipped: data/fundamental.py not found")
            return 0

    def sync_lhb_data(self, start: str = DEFAULT_START_DATE) -> int:
        """增量同步龙虎榜数据 → lhb_detail 表 (trade_date 为 YYYYMMDD 格式)。
        来源: 龙虎榜制度始于1997年3月 (沪深交易所), 取值DEFAULT_START_DATE与全项目一致。"""
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not available, skipping LHB sync")
            return 0

        conn = self._connect()
        max_date = conn.execute("SELECT MAX(trade_date) FROM lhb_detail").fetchone()[0]
        # lhb_detail.trade_date 现在统一为 YYYY-MM-DD, 与 daily.date 一致
        daily_max = conn.execute("SELECT MAX(date) FROM daily WHERE date >= '2000-01-01' AND date < '2100-01-01'").fetchone()[0]
        if max_date and daily_max and (max_date or "") >= (daily_max or ""):
            logger.info(f"lhb up to date ({max_date} >= {daily_max}), skipping")
            return 0
        # akshare API 要求 YYYYMMDD 格式 — 仅此处转换
        start = to_compact(max_date) if max_date else to_compact(DEFAULT_START_DATE)
        end = to_compact(datetime.today())

        logger.info(f"syncing LHB data: {start} → {end}")
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _fetch_lhb(s, e):
            return ak.stock_lhb_detail_em(start_date=s, end_date=e)

        df = _fetch_lhb(start, end)
        if df is None or df.empty:
            logger.info("no new LHB records")
            return 0

        conn = self._connect()
        new_count = 0
        for _, row in df.iterrows():
            sym = str(row.get("代码", "")).zfill(6)
            if len(sym) != 6:
                continue
            trade_date = to_str(row.get("上榜日", row.get("trade_date", row.get("日期", ""))))
            if not validate_date_format(trade_date, 'lhb_detail'):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO lhb_detail
                   (symbol, trade_date, close, change_pct, turnover_rate,
                    net_buy, buy_amt, sell_amt, reason)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (sym,
                 trade_date,
                 float(row.get("收盘价", 0) or 0),
                 float(row.get("涨跌幅", 0) or 0),
                 float(row.get("换手率", 0) or 0),
                 float(row.get("龙虎榜净买额", 0) or 0),
                 float(row.get("龙虎榜买入额", 0) or 0),
                 float(row.get("龙虎榜卖出额", 0) or 0),
                 str(row.get("上榜原因", "") or "")[:200])
            )
            new_count += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM lhb_detail").fetchone()[0]
        logger.info(f"LHB sync done: {new_count} new, {total} total records")
        return new_count

    def get_benchmark(self, code: str = "000300", start: str = None) -> pd.Series:
        """拉取基准指数日线，返回 (date → return) Series (小数, 非百分比)。

        优先从本地 market.db benchmark_daily 表读取。
        """
        if start is None:
            start = _require_cfg("data.benchmark_start_date")
        # 本地 market.db benchmark_daily 表
        import sqlite3, os
        from quant.config.paths import MARKET_DB
        _bm_db = MARKET_DB
        if os.path.exists(_bm_db):
            _bm_conn = sqlite3.connect(_bm_db, timeout=5)
            _bm_conn.execute("PRAGMA journal_mode=WAL")
            df = pd.read_sql_query(
                "SELECT date, close FROM benchmark_daily WHERE index_code=? AND date>=? ORDER BY date",
                _bm_conn, params=(code, start)
            )
            _bm_conn.close()
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")["close"]
                return df.pct_change().dropna()
        from quant.data.benchmark import get_benchmark_returns
        # get_benchmark_returns 返回百分比, 转小数
        bm_pct = get_benchmark_returns(code, start=start)
        if bm_pct.empty:
            return pd.Series(dtype=float, name=code)
        return bm_pct / 100.0
    def get_stock_names(self, symbols: list) -> dict:
        if not symbols:
            return {}
        placeholders = ",".join("?" for _ in symbols)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
            symbols
        ).fetchall()
        return {r[0]: r[1] for r in rows}


    def get_financials(self, symbols: list, date: str = None) -> "pd.DataFrame":
        """读取最近季度的财务报表数据(合并三表 balance + income + cash_flow)。

        symbols: 股票代码列表
        date: 交易日期 → 取最近 stat_date <= date 的季度数据
        返回: DataFrame(index=symbol, 三表合并后的所有列)
        """
        import pandas as pd

        conn = self._connect()
        if not date:
            date = datetime.today().strftime("%Y-%m-%d")

        placeholders = ",".join("?" * len(symbols))
        df = pd.DataFrame()

        for tbl in ["balance", "income", "cash_flow"]:
            sub = pd.read_sql_query(f"""
                SELECT * FROM financial_{tbl}
                WHERE (symbol, stat_date) IN (
                    SELECT symbol, MAX(stat_date)
                    FROM financial_{tbl}
                    WHERE stat_date <= date(?, '-60 days') AND symbol IN ({placeholders})
                    GROUP BY symbol
                )
            """, conn, params=[date] + symbols)

            if sub.empty:
                continue

            sub = sub.set_index("symbol")
            if df.empty:
                df = sub
            else:
                # 只合并新列，不用 rsuffix，避免 stat_date_dup 冲突
                cols_to_add = [c for c in sub.columns if c not in df.columns]
                if cols_to_add:
                    df = df.join(sub[cols_to_add], how="outer")

        return df


    def get_fundamentals(self, symbols: list = None, date: str = None) -> pd.DataFrame:
        """读取基本面数据: PE, PB, 总市值, ROE, 行业, 52周高点, 最新收盘价。

        symbols: 股票列表, None = 全部
        date: 交易日期, 用于获取当日最新收盘价(high52w_dist 因子需要)
        返回: DataFrame(index=symbol, columns=[pe,pb,total_mv,roe,industry,high_52w,close_latest])
        """
        conn = self._connect()
        base_cols = "symbol, pe, pe_ttm, pb, total_mv, roe, industry, high_52w, eps, bvps"
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql_query(
                f"SELECT {base_cols} FROM stocks WHERE symbol IN ({placeholders})",
                conn, params=symbols)
        else:
            df = pd.read_sql_query(
                f"SELECT {base_cols} FROM stocks", conn)
        df = df.set_index("symbol")
        # 过滤负值和极端PE/PB (PE>1000=数据噪声, 无alpha价值)
        df.loc[df["pe"] <= 0, "pe"] = None
        df.loc[df["pe"] > 1000, "pe"] = None
        df.loc[df["pb"] <= 0, "pb"] = None
        # 如果有 date: 严格 PIT — 估值字段只认 ≤ date 的最近一个 daily_valuation
        # 交易日, 不回退 stocks 快照 (快照=最新值, 历史日期使用即前视,
        # 2026-07-26 审计 P0-4: 07-03 覆盖截止后 20 天物化行被快照污染)。
        # 覆盖外日期 → NaN → 因子按缺失处理 (诚实缺数据, 不静默前视)。
        if date:
            val_df = pd.read_sql_query(
                "SELECT symbol, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap, turnover_rate "
                "FROM daily_valuation "
                "WHERE date = (SELECT MAX(date) FROM daily_valuation WHERE date <= ?)",
                conn, params=(date,))
            for col in ["pe", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "total_mv", "roe"]:
                if col in df.columns:
                    df[col] = None
            if not val_df.empty:
                val_df = val_df.set_index("symbol")
                df["pe_ttm"] = val_df["pe_ttm"]
                df["pb"] = val_df["pb"]
                df["ps_ttm"] = val_df["ps_ttm"]
                df["pcf_ttm"] = val_df["pcf_ttm"]
                if "market_cap" in val_df.columns:
                    # JQData market_cap 单位是亿元, akshare total_mv 是元 → 统一到元
                    df["total_mv"] = val_df["market_cap"] * 1e8
                df["pe"] = val_df["pe_ttm"]  # compute_ep_ratio 优先 pe_ttm
            # 覆盖后重过滤 (与快照路径同口径)
            df.loc[df["pe"] <= 0, "pe"] = None
            df.loc[df["pe"] > 1000, "pe"] = None
            df.loc[df["pb"] <= 0, "pb"] = None
            # 加入最新收盘价
            df_date = pd.read_sql_query(
                "SELECT symbol, close FROM daily WHERE date=?", conn, params=(date,))
            df_date = df_date.set_index("symbol").rename(columns={"close": "close_latest"})
            df = df.join(df_date, how="left")
        else:
            df["close_latest"] = None

        # P2-2: derive ROE from PB/PE when roe column is NULL
        null_roe = df["roe"].isna() | (df["roe"] <= 0)
        if null_roe.any():
            derived = df["pb"] / df["pe"].replace(0, None)
            derived = derived.where((derived > 0) & (derived < _require_cfg("data.derived_ratio_max")))
            df.loc[null_roe, "roe"] = derived.loc[null_roe]

        # high52w: compute from daily table (MAX close over 252 trading days)
        if date:
            df_high52 = pd.read_sql_query(
                "SELECT symbol, MAX(close) as high_52w FROM daily WHERE date >= date(?, '-244 days') AND date <= ? GROUP BY symbol",
                conn, params=(date, date))
            df_high52 = df_high52.set_index("symbol")
            df["high_52w"] = df_high52["high_52w"]

        return df


if __name__ == "__main__":
    import os
    store = DataStore(
        tushare_token=os.environ.get("TUSHARE_TOKEN", "")
    )

    # 1. 同步股票列表
    print("=== 同步股票列表 ===")
    store.sync_stock_list()

    # 2. 增量更新日线（首次会全量拉取）
    print("\n=== 增量更新日线 ===")
    store.update_daily(start=DEFAULT_START_DATE)

    # 3. 验证
    print("\n=== 数据统计 ===")
    stats = store.get_stock_count()
    for k, v in stats.items():
        print(f"  {k}: {v}")


def market_conn(mode='ro'):
    """统一数据库连接 — 自动 WAL + busy_timeout=30s.
    mode: 'ro' = read-only (附加 read_uncommitted), 'rw' = read-write.
    """
    _db = os.path.join(os.path.dirname(__file__), "market.db")
    _c = DatabaseManager.get_connection(_db)
    _c.execute("PRAGMA journal_mode=WAL")
    _c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    if mode == 'ro':
        _c.execute("PRAGMA read_uncommitted=1")
    return _c
