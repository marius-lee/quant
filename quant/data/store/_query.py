from datetime import datetime
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.data.duckdb_store import get_duckdb_proxy
from quant.utils.date import DEFAULT_START_DATE
from quant.utils.date import to_str
import pandas as pd
"""Mixin class — extracted from DataStore."""

class DataStoreQueryMixin:
    """Mixin with 9 methods."""

def get_universe(self, date_str: str = None):
        """Get point-in-time stock universe for a given date.

        Only includes stocks that were:
          - Listed on or before date_str
          - Not yet delisted (delist_date is NULL or after date_str)
          - Non-Beijing Exchange

        This eliminates survivorship bias in backtesting.
        """
        conn = self._connect()
        # B1 (2026-08-18): list_date/delist_date 存储为 ISO (YYYY-MM-DD, DB 实证
        # 5556 行 ISO), 原 P0-1 修复用 strftime('%Y%m%d') 转 compact 再比较 —
        # ISO vs compact 字典序在 '-' (0x2D) vs '0' (0x30) 处错位, 同年内恒真
        # → 实测 2024-06-15 查询错误包含 46 只未来上市股票 (前视).
        # 修复: 按存储格式分支比较, 两种格式均正确处理.
        query = (
            "SELECT symbol FROM stocks "
            "WHERE ((list_date LIKE '%-%' AND list_date <= ?) "
            "   OR  (list_date NOT LIKE '%-%' AND list_date <= strftime('%Y%m%d', ?))) "
            "  AND (delist_date IS NULL OR delist_date = '' "
            "   OR (delist_date LIKE '%-%' AND delist_date > ?) "
            "   OR (delist_date NOT LIKE '%-%' AND delist_date > strftime('%Y%m%d', ?))) "
            "  AND market != 'BJ'"
        )
        if date_str is None:
            from datetime import date
            date_str = date.today().strftime("%Y-%m-%d")
        rows = conn.execute(query, (date_str, date_str, date_str, date_str)).fetchall()
        return [r[0] for r in rows]

def get_daily(self, symbols: list, start: str = DEFAULT_START_DATE,
                  end: str = None, columns: list = None) -> pd.DataFrame:
        """读取日线数据 (v435: 优先 DuckDB 列式并行查询, 回退 SQLite).

        columns: 需要的列，默认全部。可只传 ['close','volume'] 节省 IO。
        自动分块避免 SQLite 的 999 参数上限。
        结果缓存: 同一次 DataStore 实例内相同参数只查一次 DB。"""
        # v435: 优先使用 DuckDB 列式并行查询
        try:
            duckdb_proxy = get_duckdb_proxy()
            # DuckDB 支持大量参数，无需分块
            df = duckdb_proxy.get_daily(symbols, start, end, columns)
            if df is not None and not df.empty:
                return df
            logger.warning("DuckDB returned empty DataFrame, fallback to SQLite")
        except Exception as e:
            logger.warning(f"DuckDB query failed, fallback to SQLite: {e}")

        # 回退: 原 SQLite 逻辑
        MAX_SYMBOLS = 900
        _ck = (hash(tuple(sorted(symbols))), start, end, tuple(columns or []))
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

def get_stock_names(self, symbols: list) -> dict:
        if not symbols:
            return {}
        # v391: 回测 1700 次调相同数据, 缓存避免重复 DB 查询
        _key = frozenset(symbols)
        if not hasattr(self, '_stock_names_cache'):
            self._stock_names_cache = {}
        if _key in self._stock_names_cache:
            return self._stock_names_cache[_key]
        placeholders = ",".join("?" for _ in symbols)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
            symbols
        ).fetchall()
        result = {r[0]: r[1] for r in rows}
        self._stock_names_cache[_key] = result
        return result

def get_financials(self, symbols: list, date: str = None) -> "pd.DataFrame":
        """读取最近季度的财务报表数据(合并三表 balance + income + cash_flow)。

        symbols: 股票代码列表
        date: 交易日期 → 取最近 stat_date <= date 的季度数据
        返回: DataFrame(index=symbol, 三表合并后的所有列)

        PIT (2026-08-18): 原 `stat_date <= date(?, '-60 days')` — 年报披露时滞
        最长 120 天, -60 天窗口内未披露财报已被使用 → 基本面因子回测最长
        2 个月前视. 现双分支:
          - 真实公告日行 (pub_date != stat_date, tushare 源): pub_date <= date
          - 代填行 (pub_date IS NULL 或 = stat_date, sina 源无公告日):
            stat_date + 披露滞后上限 (年报 120 / 半年报 62 / 季报 45 天,
            证监会披露规则, config data.financials.disclosure_lag_days).
        """
        import pandas as pd

        conn = self._connect()
        if not date:
            date = datetime.today().strftime("%Y-%m-%d")

        placeholders = ",".join("?" * len(symbols))
        _lag = _require_cfg("data.financials.disclosure_lag_days")
        _lag_a = int(_lag["annual"]); _lag_s = int(_lag["semi_annual"]); _lag_q = int(_lag["quarterly"])
        df = pd.DataFrame()

        # 表名与 tushare 接口名一致: financial_cashflow (无下划线, 2026-08-17 修复)
        for tbl in ["balance", "income", "cashflow"]:
            sub = pd.read_sql_query(f"""
                SELECT * FROM financial_{tbl}
                WHERE (symbol, stat_date) IN (
                    SELECT symbol, MAX(stat_date)
                    FROM financial_{tbl}
                    WHERE symbol IN ({placeholders})
                      AND (
                        (pub_date IS NOT NULL AND pub_date != stat_date AND pub_date <= ?)
                        OR (pub_date IS NULL OR pub_date = stat_date)
                           AND stat_date <= date(?, '-' || CASE strftime('%m', stat_date)
                                WHEN '12' THEN ? WHEN '06' THEN ? ELSE ? END || ' days')
                      )
                    GROUP BY symbol
                )
            """, conn, params=symbols + [date, date, str(_lag_a), str(_lag_s), str(_lag_q)])

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
                "SELECT symbol, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap, turnover_rate, source "
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
                    # P0-2 fix: 三源三单位 — eastmoney 写元, jqdata 写万元, tushare 写万元.
                    # 原代码无条件 ×1e8 导致 eastmoney 值 1e8 倍放大, jqdata 值 1e4 倍放大.
                    mc = val_df["market_cap"]
                    src = val_df["source"] if "source" in val_df.columns else None
                    if src is not None:
                        conv = pd.Series(1.0, index=mc.index)
                        conv[src == "jqdata"] = 1e4    # 万元→元
                        conv[src == "tushare"] = 1e4    # 万元→元
                        # eastmoney 默认 1.0 (已是元)
                        df["total_mv"] = mc * conv
                    else:
                        # 历史无 source 列: 原行为 (假设 jqdata, ×1e4 更符合实测)
                        df["total_mv"] = mc * 1e4
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
