from datetime import datetime
from quant.utils.date import DEFAULT_START_DATE
from quant.utils.date import to_compact
from quant.utils.date import to_str
from quant.utils.date import validate_date_format
"""Mixin class — extracted from DataStore."""

class DataStoreSync2Mixin:
    """Mixin with 4 methods."""

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
                # v552: 每股立即 commit — 原最后一次性 commit, symbols=None 全表
                # 时单事务写全历史 daily (5000 只 × 秒级 = 分钟级长锁)
                conn.commit()
                logger.info(f"rebase: {sym} factor {f_old:.4f}→{f_new:.4f}, "
                            f"history × {ratio:.6f}")
        conn.commit()
        return rebased

def sync_fundamentals(self) -> int:
        """同步 PE/PB/市值 — 批量PE+市值, 逐只补PB, 多源容错"""
        try:
            from quant.data.fundamental import sync_all
            _fund_conn = self._connect()
            try:
                result = sync_all(_fund_conn, max_fetch=-1)
            finally:
                _fund_conn.close()
            logger.info(f"fundamentals: PE/PB/市值 updated count={result['count']}")
            return result["count"]
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
