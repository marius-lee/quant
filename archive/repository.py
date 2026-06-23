"""数据访问层 — 所有 SQLite 查询收口于此。

外部代码通过 StockRepo / PriceRepo 访问数据，不再写原始 SQL。
"""

import pandas as pd
from data.store import DataStore
from utils.logger import get_logger
from utils.date import DEFAULT_START_DATE

logger = get_logger("data.repository")


class StockRepo:
    """股票列表 + 基本面数据查询"""

    def __init__(self, store: DataStore):
        self.store = store

    def get_qualified(self, min_days: int = None, capital: float = None) -> list:
        """返回合格股票列表（可配置ST/次新过滤，资金规模自适应可买性过滤）。

        max_price: capital / 100 (最低1手=100股)
        min_daily_amount: (capital / max_positions) × 10
          来源: 单笔持仓≤日均成交额10% (Portfolio123社区标准; QuantConnect实践)
          例如 ¥5000÷3×10=¥16,667 — 小资金可进入机构无法交易的低流动性池

        config.yaml 的 affordable.* 值作为 capital=None 时的静态回退。
        """
        from config.loader import get as cfg
        if min_days is None:
            min_days = cfg("affordable.min_history_days", 120)
        exclude_st = cfg("affordable.exclude_st", False)
        exclude_star_st = cfg("affordable.exclude_star_st", True)

        if capital is not None and capital > 0:
            max_positions = cfg("backtest.max_positions", 3)
            max_price = capital / 100  # ¥5000÷100=¥50
            min_amount = (capital / max_positions) * 10  # 单笔×10
        else:
            max_price = cfg("affordable.max_stock_price", 30)
            min_amount = cfg("affordable.min_daily_amount", 5_000_000)

        conn = self.store._connect()

        conditions = ["1=1"]
        if exclude_st and exclude_star_st:
            conditions.append("(s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%')")
        elif exclude_star_st:
            conditions.append("(s.name NOT LIKE '%*ST%' AND s.name NOT LIKE '%退%')")
        elif exclude_st:
            # 排除 ST 但保留 *ST: NOT (name LIKE '%ST%' AND name NOT LIKE '%*ST%')
            # = name NOT LIKE '%ST%' OR name LIKE '%*ST%'
            conditions.append("(s.name NOT LIKE '%ST%' OR s.name LIKE '%*ST%')")
            conditions.append("s.name NOT LIKE '%退%'")

        where_clause = " AND ".join(conditions)

        # 拿每只股票各自的最新日期做可买性过滤 (不依赖全局 MAX(date))
        symbols = [r[0] for r in conn.execute(f"""
            SELECT d.symbol FROM daily d
            INNER JOIN stocks s ON s.symbol = d.symbol
            WHERE {where_clause}
            GROUP BY d.symbol HAVING COUNT(*) >= ?
            ORDER BY d.symbol
        """, (min_days,)).fetchall()]
        n_after_days = len(symbols)
        logger.info(f"stock filter: {n_after_days} with >= {min_days} days")

        # 可买性过滤: 股价和日成交额约束 (每只股票用自己最新的日期)
        if not symbols:
            return []
        if max_price > 0 or min_amount > 0:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql_query(
                f"""SELECT d.symbol, d.close, d.amount FROM daily d
                    WHERE d.symbol IN ({placeholders})
                    AND d.date = (SELECT MAX(date) FROM daily WHERE symbol = d.symbol)
                """, conn, params=symbols
            )
            affordable = set()
            for _, row in df.iterrows():
                if max_price > 0 and row["close"] > max_price:
                    continue
                if min_amount > 0 and row["amount"] < min_amount:
                    continue
                affordable.add(row["symbol"])
            n_after_affordable = len(affordable)
            logger.info(f"stock filter: ... → {n_after_affordable} with price<={max_price}, amount>={min_amount}")
            symbols = [s for s in symbols if s in affordable]

        logger.info(f"stock filter: {len(symbols)} final qualified")
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
        """symbol → industry + total_mv。若 industry 列不存在则返回空。

        TODO: industry 列需要从外部数据源（如东方财富/申万行业分类）同步创建。
        当前 stocks 表无 industry 列，此方法始终返回空 DataFrame。
        """
        conn = self.store._connect()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()]
        if "industry" not in cols:
            logger.warning("industry column not available — get_industry_mv returns empty")
            return pd.DataFrame()
        return self._query_symbols("symbol,industry,total_mv", symbols)

    def _query_symbols(self, cols: str, symbols: list) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        conn = self.store._connect()
        ph = ",".join("?" for _ in symbols)
        df = pd.read_sql_query(
            f"SELECT {cols} FROM stocks WHERE symbol IN ({ph})",
            conn, params=symbols
        )
        return df


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
        return self.store.get_daily(symbols, start=start or DEFAULT_START_DATE)
