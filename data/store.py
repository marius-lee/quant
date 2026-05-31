"""SQLite 数据仓库 — 全A股 + 增量更新。

    首次: 下载全部A股列表 + 全部历史日线 → SQLite
    后续: 对比 SQLite 已有数据，只拉取增量日期
"""

import sqlite3
import time
from datetime import datetime

import pandas as pd

from utils.logger import get_logger
logger = get_logger("data.store")


def _ts_code(sym: str) -> str:
    return f"{sym}.SH" if sym.startswith(("6", "9", "68")) else f"{sym}.SZ"


class DataStore:
    """全A股 SQLite 数据仓库"""

    def __init__(self, db_path: str = "data/market.db",
                 tushare_token: str = ""):
        self.db_path = db_path
        self.token = tushare_token
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol    TEXT PRIMARY KEY,
                name      TEXT,
                market    TEXT,
                list_date TEXT
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
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        # 验证 WAL 是否实际生效
        actual_journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if actual_journal.lower() != "wal":
            logger.warning(f"WAL mode not active on {self.db_path}: got {actual_journal}")
        return conn

    # ============================================================
    # 股票列表
    # ============================================================

    def sync_stock_list(self) -> int:
        """拉取全A股列表。优先 tushare，失败回退 akshare（免费无频率限制）。"""
        conn = self._connect()
        existing = set(
            r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()
        )

        # 尝试 tushare
        try:
            import tushare as ts
            ts.set_token(self.token)
            pro = ts.pro_api()
            df = pro.stock_basic(exchange="", list_status="L",
                fields="ts_code,symbol,name,list_date,market")
            if df is not None and not df.empty:
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
                conn.close()
                logger.info(f"stock list (tushare): {total} total")
                return total
        except Exception as e:
            logger.warning(f"stock list tushare failed: {e}, trying akshare")

        # 回退 akshare
        try:
            import akshare as ak
            df = ak.stock_info_a_code_name()
            new_count = 0
            for _, row in df.iterrows():
                sym = str(row.get("code", row.get("item_code", ""))).zfill(6)
                name = row.get("name", "")
                if sym not in existing and len(sym) == 6:
                    market = "SH" if sym.startswith(("6","9","68")) else "SZ"
                    conn.execute(
                        "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                        (sym, name, market, ""))
                    new_count += 1
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
            conn.close()
            logger.info(f"stock list (akshare): {total} total ({new_count} new)")
            return new_count
        except Exception as e:
            logger.warning(f"stock list akshare also failed: {e}")
            conn.close()
            return 0

    # ============================================================
    # 日线数据 — 增量更新
    # ============================================================

    def update_daily(self, symbols: list = None,
                     start: str = None) -> int:
        """增量更新日线。只拉取 SQLite 中没有的日期。

        symbols: None 表示全部 stocks 表中的股票
        返回: 新写入的行数
        """
        if start is None:
            from config.loader import get as cfg
            start = cfg("data.start_date", "20200101")
        import tushare as ts
        ts.set_token(self.token)
        pro = ts.pro_api()

        conn = self._connect()

        # 获取要更新的股票列表
        if symbols is None:
            symbols = [
                r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()
            ]

        total_new = 0
        fail_count = 0
        batch_size = 10  # 每批 10 只，保证不超 tushare 6000 行限制

        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i:i + batch_size]
            # 查该批股票在DB中的最大日期，只拉取新数据（真正增量）
            batch_max = conn.execute(
                f"SELECT MAX(date) FROM daily WHERE symbol IN ({','.join('?' for _ in chunk)})",
                chunk
            ).fetchone()[0]
            batch_start = (batch_max if batch_max and batch_max > start.replace("-", "")
                           else start.replace("-", ""))
            ts_codes = ",".join(_ts_code(s) for s in chunk)

            try:
                df = pro.daily(
                    ts_code=ts_codes,
                    start_date=batch_start,
                    end_date=datetime.today().strftime("%Y%m%d"),
                )
                if df is None or df.empty:
                    continue

                # 批量插入: executemany 比逐行 insert 快 10x+
                rows = []
                for _, row in df.iterrows():
                    rows.append((
                        row["ts_code"].split(".")[0], row["trade_date"],
                        float(row.get("open", 0)), float(row.get("high", 0)),
                        float(row.get("low", 0)), float(row["close"]),
                        float(row.get("vol", 0)), float(row.get("amount", 0)), 0.0
                    ))
                conn.executemany(
                    """INSERT OR IGNORE INTO daily
                       (symbol,date,open,high,low,close,volume,amount,turnover)
                       VALUES (?,?,?,?,?,?,?,?,?)""", rows
                )
                total_new += len(rows)
            except Exception as e:
                fail_count += 1
                if fail_count <= 3:
                    logger.warning(f"daily fetch failed at batch {i}: {e}")

            if i % 100 == 0 and i > 0:
                conn.commit()
                logger.info(f"daily update: {i}/{len(symbols)} stocks, {total_new} new rows")

            time.sleep(0.4)  # tushare 频率限制

        conn.commit()
        if fail_count > 0:
            total_batches = (len(symbols) + batch_size - 1) // batch_size
            logger.error(f"daily update: {fail_count}/{total_batches} batches failed")
        total_rows = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        conn.close()
        logger.info(f"daily total: {total_rows} rows ({total_new} new)")
        return total_new

    # ============================================================
    # 读取数据
    # ============================================================

    def get_daily(self, symbols: list, start: str = "20200101",
                  end: str = None) -> pd.DataFrame:
        """从 SQLite 读取日线，返回 (dates × stocks) 宽表 DataFrame。
        自动分块避免 SQLite 的 999 参数上限。"""
        MAX_SYMBOLS = 900
        if len(symbols) <= MAX_SYMBOLS:
            return self._get_daily_chunk(symbols, start, end)

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

    def _get_daily_chunk(self, symbols: list, start: str = "20200101",
                          end: str = None) -> pd.DataFrame:
        end = end or datetime.today().strftime("%Y%m%d")
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
        conn.close()
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        return df.pivot(index="date", columns="symbol", values=[
            "open", "high", "low", "close", "volume", "amount", "turnover"
        ])

    def get_stock_count(self) -> dict:
        conn = self._connect()
        n_stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        n_daily = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM daily"
        ).fetchone()
        conn.close()
        return {
            "stocks": n_stocks,
            "daily_rows": n_daily,
            "date_min": date_range[0],
            "date_max": date_range[1],
            "trading_days": date_range[2],
        }

    def sync_fundamentals(self) -> int:
        """同步 PE/PB/市值 — 批量PE+市值, 逐只补PB, 多源容错"""
        from data.fundamental import sync_all
        result = sync_all(self.db_path, max_pb_fetch=-1)
        logger.info(f"fundamentals: PE={result['pe_count']} PB={result['pb_count']}")
        return result["pe_count"]

    def get_benchmark(self, code: str = "000300", start: str = None) -> pd.Series:
        """拉取基准指数日线，返回 (date → return) Series"""
        if start is None:
            from config.loader import get as cfg
            start = cfg("data.start_date", "20200101")
        import tushare as ts
        ts.set_token(self.token)
        pro = ts.pro_api()
        try:
            df = pro.index_daily(ts_code=f"{code}.SH", start_date=start.replace('-',''),
                                end_date=datetime.today().strftime("%Y%m%d"),
                                fields="trade_date,close")
            if df is None or df.empty:
                return pd.Series()
            df = df.sort_values("trade_date")
            df["date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("date")["close"]
            return df.pct_change().dropna()
        except Exception:
            logger.warning(f"benchmark {code} fetch failed")
            return pd.Series()

    def get_stock_names(self, symbols: list) -> dict:
        if not symbols:
            return {}
        placeholders = ",".join("?" for _ in symbols)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
            symbols
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}


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
    store.update_daily(start="20200101")

    # 3. 验证
    print("\n=== 数据统计 ===")
    stats = store.get_stock_count()
    for k, v in stats.items():
        print(f"  {k}: {v}")
