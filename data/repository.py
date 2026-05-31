"""数据访问层 — 所有 SQLite 查询收口于此。

外部代码通过 StockRepo / FactorRepo 访问数据，不再写原始 SQL。
"""

import pandas as pd
from data.store import DataStore
from utils.logger import get_logger

logger = get_logger("data.repository")


class StockRepo:
    """股票列表 + 基本面数据查询"""

    def __init__(self, store: DataStore):
        self.store = store

    def get_qualified(self, min_days: int = None) -> list:
        """返回合格股票列表（可配置ST/次新过滤，5000元资金可买性过滤）。

        config.yaml 控制:
          - affordable.min_history_days: 最少交易日 (默认120，不排除次新)
          - affordable.exclude_st: 是否排除ST (默认false，ST摘帽是弹性来源)
          - affordable.exclude_star_st: 是否排除*ST (默认true)
          - affordable.max_stock_price: 最高股价 (默认30元，5000元买得起)
          - affordable.min_daily_amount: 最低日成交额 (默认500万)
        """
        from config.loader import get as cfg
        if min_days is None:
            min_days = cfg("affordable.min_history_days", 120)
        exclude_st = cfg("affordable.exclude_st", False)
        exclude_star_st = cfg("affordable.exclude_star_st", True)
        max_price = cfg("affordable.max_stock_price", 30)
        min_amount = cfg("affordable.min_daily_amount", 5_000_000)

        conn = self.store._connect()

        conditions = ["1=1"]
        if exclude_st and exclude_star_st:
            conditions.append("(s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%*ST%' AND s.name NOT LIKE '%退%')")
        elif exclude_star_st:
            conditions.append("(s.name NOT LIKE '%*ST%' AND s.name NOT LIKE '%退%')")
        elif exclude_st:
            conditions.append("s.name NOT LIKE '%ST%'")

        where_clause = " AND ".join(conditions)

        # 获取最近日期的收盘价用于可买性过滤
        latest_date = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if not latest_date:
            conn.close()
            return []

        symbols = [r[0] for r in conn.execute(f"""
            SELECT d.symbol FROM daily d
            INNER JOIN stocks s ON s.symbol = d.symbol
            WHERE {where_clause}
            GROUP BY d.symbol HAVING COUNT(*) >= ?
            ORDER BY d.symbol
        """, (min_days,)).fetchall()]

        # 可买性过滤: 股价和日成交额约束
        if max_price > 0 or min_amount > 0:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql_query(
                f"""SELECT symbol, close, amount FROM daily
                    WHERE symbol IN ({placeholders}) AND date = ?
                """, conn, params=symbols + [latest_date]
            )
            affordable = set()
            for _, row in df.iterrows():
                if max_price > 0 and row["close"] > max_price:
                    continue
                if min_amount > 0 and row["amount"] < min_amount:
                    continue
                affordable.add(row["symbol"])
            symbols = [s for s in symbols if s in affordable]

        conn.close()
        return symbols

    def get_fundamentals(self, symbols: list) -> pd.DataFrame:
        """读取 PE/PB/市值/股息/52周/换手率"""
        cols = "symbol,pe,pe_ttm,pb,total_mv,div_yield,cfps,high_52w,low_52w,turnover_rate"
        return self._query_symbols(cols, symbols)

    def get_names(self, symbols: list) -> dict:
        """symbol → name 映射"""
        df = self._query_symbols("symbol,name", symbols)
        return dict(zip(df["symbol"], df["name"])) if not df.empty else {}

    def get_industry_mv(self, symbols: list) -> pd.DataFrame:
        """symbol → industry + total_mv。若 industry 列不存在则返回空。"""
        conn = self.store._connect()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()]
        conn.close()
        if "industry" not in cols:
            return pd.DataFrame()
        return self._query_symbols("symbol,industry,total_mv", symbols)

    def _query_symbols(self, cols: str, symbols: list) -> pd.DataFrame:
        conn = self.store._connect()
        ph = ",".join("?" for _ in symbols)
        df = pd.read_sql_query(
            f"SELECT {cols} FROM stocks WHERE symbol IN ({ph})",
            conn, params=symbols
        )
        conn.close()
        return df


class FactorRepo:
    """因子缓存（factors_cache 表）读写"""

    def __init__(self, store: DataStore):
        self.store = store

    def load_batch(self, symbols: list, start_date: str = None,
                   end_date: str = None) -> pd.DataFrame:
        """读取一批股票的全部历史因子，返回 (date,stock) × factor。

        start_date/end_date: YYYY-MM-DD 格式，限制日期范围以控制内存。
        自动分块以避免 SQLite 的 999 参数上限。
        """
        MAX_PARAMS = 900
        if len(symbols) <= MAX_PARAMS:
            return self._load_batch_chunk(symbols, start_date, end_date)
        frames = []
        for i in range(0, len(symbols), MAX_PARAMS):
            df = self._load_batch_chunk(symbols[i:i + MAX_PARAMS], start_date, end_date)
            if not df.empty:
                frames.append(df)
        return pd.concat(frames) if frames else pd.DataFrame()

    def _load_batch_chunk(self, symbols: list, start_date: str = None,
                          end_date: str = None) -> pd.DataFrame:
        # 统一日期格式: factors_cache 存 YYYY-MM-DD 字符串 (pandas to_sql 生成)
        def _norm_date(d):
            if d is None:
                return None
            s = str(d)
            if "-" not in s and len(s) == 8:
                return f"{s[:4]}-{s[4:6]}-{s[6:]}"  # YYYYMMDD → YYYY-MM-DD
            return s
        start_date = _norm_date(start_date)
        end_date = _norm_date(end_date)
        conn = self.store._connect()
        ph = ",".join("?" for _ in symbols)
        params = symbols[:]
        where = f"stock IN ({ph})"
        if start_date and end_date and start_date == end_date:
            where += " AND date LIKE ?"
            params.append(f"{start_date}%")
        else:
            if start_date:
                where += " AND date >= ?"
                params.append(start_date)
            if end_date:
                where += " AND date < ?"
                from datetime import datetime, timedelta
                end_next = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                params.append(end_next)
        df = pd.read_sql_query(
            f"SELECT * FROM factors_cache WHERE {where} ORDER BY date",
            conn, params=params
        )
        conn.close()
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        fc = [c for c in df.columns if c not in ("date", "stock")]
        return df.set_index(["date", "stock"])[fc]

    def save_batch(self, df: pd.DataFrame, mode: str = "append"):
        """写入一批因子数据"""
        conn = self.store._connect()
        df.to_sql("factors_cache", conn, if_exists=mode, index=False, chunksize=30000)
        conn.commit()
        conn.close()

    def max_date(self) -> str:
        """缓存中最新的日期"""
        conn = self.store._connect()
        try:
            return conn.execute("SELECT MAX(date) FROM factors_cache").fetchone()[0]
        except Exception:
            logger.warning("failed to read factors_cache max date")
            return None
        finally:
            conn.close()

    def ensure_index(self):
        conn = self.store._connect()
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fc_uniq ON factors_cache(stock,date)")
        conn.commit()
        conn.close()


class PriceRepo:
    """日线价格数据查询"""

    def __init__(self, store: DataStore):
        self.store = store

    def get_close(self, symbols: list) -> pd.DataFrame:
        """返回 (dates × stocks) 收盘价 DataFrame"""
        raw = self.store.get_daily(symbols)
        return raw["close"].sort_index().dropna(how="all") if not raw.empty else pd.DataFrame()

    def get_ohlcv(self, symbols: list, start: str = None) -> dict:
        """返回原始 MultiIndex DataFrame（含 open/high/low/close/volume/amount）"""
        return self.store.get_daily(symbols, start=start or "20200101")
