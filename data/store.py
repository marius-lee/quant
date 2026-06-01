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

    def __init__(self, db_path: str = "data/market.db",
                 tushare_token: str = ""):
        self.db_path = db_path
        self.token = tushare_token
        self._conn = None
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
        """)
        conn.commit()

    def _connect(self):
        """获取共享连接。首次创建，之后复用同一条。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA cache_size=-64000")
        return self._conn

    def close(self):
        """关闭数据库连接。任务结束时调用。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

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
        except Exception as e:
            logger.warning(f"stock list akshare also failed: {e}")
            return 0

    # ============================================================
    # 日线数据 — 增量更新（tushare 优先，失败回退 akshare）
    # ============================================================

    @staticmethod
    def _norm_row(sym: str, date: str, o: float, h: float, l: float, c: float,
                  vol: float, amt: float, turnover: float = 0.0) -> tuple:
        """标准化一行日线数据: 成交量→手, 成交额→千元, 精度4位小数。"""
        return (sym, date, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
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

    def _fetch_batch_tushare(self, pro, ts_codes: str, batch_start: str):
        """tushare 批量获取日线: vol=手, amt=千元 ✅ 无需换算"""
        df = pro.daily(
            ts_code=ts_codes,
            start_date=batch_start,
            end_date=datetime.today().strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return None
        rows = []
        for _, row in df.iterrows():
            rows.append(self._norm_row(
                row["ts_code"].split(".")[0], row["trade_date"],
                float(row.get("open", 0)), float(row.get("high", 0)),
                float(row.get("low", 0)), float(row.get("close", 0)),
                float(row.get("vol", 0)), float(row.get("amount", 0)),
                float(row.get("turnover_rate", 0) or 0)))
        logger.info(f"[tushare] {ts_codes}: {len(rows)} rows (vol=手, amt=千元)")
        return rows

    def _fetch_tencent_daily(self, symbols: list, start_date: str) -> list:
        """腾讯财经逐只日线: vol=股→/100→手, amt用close×vol估算(元→/1000→千元)"""
        import urllib.request, json as _json
        max_days = 2000
        rows = []
        for sym in symbols:
            try:
                market = _tencent_market(sym)
                url = (f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                       f"?param={market}{sym},day,,,{max_days},qfq")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=15)
                data = _json.loads(resp.read().decode("utf-8"))
                kline = data.get("data", {}).get(f"{market}{sym}", {}).get("qfqday")
                if not kline:
                    continue
                for row in kline:
                    d = str(row[0]).replace("-", "")  # YYYY-MM-DD → YYYYMMDD
                    if d < start_date:
                        continue
                    c = float(row[2])          # close
                    vol_raw = float(row[5])     # 股
                    amt_raw = c * vol_raw       # 元 (=close×volume)
                    rows.append(self._norm_row(
                        sym, d.replace("-", ""),
                        float(row[1]), float(row[3]), float(row[4]), c,
                        vol_raw / 100,          # 股 → 手
                        amt_raw / 1000,         # 元 → 千元
                        0.0))
            except Exception:
                continue
        if rows:
            logger.info(f"[tencent] {len(symbols)} stocks: {len(rows)} rows (vol/100→手, amt/1000→千元)")
        return rows

    def _fetch_akshare_daily(self, symbols: list, start_date: str) -> list:
        """akshare 逐只日线: vol=手, amt=元 →/1000→千元, 唯一有历史换手率✅"""
        try:
            import akshare as ak
        except ImportError:
            raise RuntimeError("akshare not installed")
        rows = []
        end_date = datetime.today().strftime("%Y%m%d")
        for sym in symbols:
            try:
                df = ak.stock_zh_a_hist(
                    symbol=sym, period="daily",
                    start_date=start_date, end_date=end_date, adjust="")
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append(self._norm_row(
                        str(row["股票代码"]),
                        str(row["日期"]).replace("-", ""),
                        float(row.get("开盘", 0) or 0), float(row.get("最高", 0) or 0),
                        float(row.get("最低", 0) or 0), float(row.get("收盘", 0) or 0),
                        float(row.get("成交量", 0) or 0),          # 手 ✅
                        float(row.get("成交额", 0) or 0) / 1000,   # 元→千元
                        float(row.get("换手率", 0) or 0)))
                time.sleep(1.5)
            except Exception:
                continue
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
        end_date = datetime.today().strftime("%Y%m%d")
        for sym in symbols:
            try:
                ts_code = _ts_code(sym)
                df = api.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append(self._norm_row(
                        sym, str(row["trade_date"])[:10].replace("-", ""),
                        float(row.get("open", 0) or 0), float(row.get("high", 0) or 0),
                        float(row.get("low", 0) or 0), float(row.get("close", 0) or 0),
                        float(row.get("vol", 0) or 0), float(row.get("amount", 0) or 0), 0.0))
            except Exception:
                continue
        if rows:
            logger.info(f"[zzshare] {len(symbols)} stocks: {len(rows)} rows (vol=手, amt=千元)")
        return rows

    def _fetch_tickflow_daily(self, symbols: list, start_date: str = None) -> list:
        """TickFlow 批量日线: vol=手✅, amt=元❌→/1000→千元"""
        try:
            from tickflow import TickFlow
            tf = TickFlow.free()
        except ImportError:
            raise RuntimeError("tickflow not installed (pip install tickflow)")
        rows = []
        codes = [f"{s}.SH" if s.startswith(("6","9","68")) else f"{s}.SZ" for s in symbols]
        try:
            dfs = tf.klines.batch(codes, period="1d", count=10000, as_dataframe=True, show_progress=False)
        except Exception:
            # 回退到逐只
            dfs = {}
            for code in codes:
                try:
                    df = tf.klines.get(code, period="1d", count=10000, as_dataframe=True)
                    if not df.empty:
                        dfs[code] = df
                except Exception:
                    continue
        for code, df in dfs.items():
            if df.empty:
                continue
            sym = code.split(".")[0]
            for _, row in df.iterrows():
                d = str(row.get("trade_date", ""))[:10].replace("-", "")
                if len(d) != 8:
                    continue
                rows.append(self._norm_row(
                    sym, d,
                    float(row.get("open", 0) or 0), float(row.get("high", 0) or 0),
                    float(row.get("low", 0) or 0), float(row.get("close", 0) or 0),
                    float(row.get("volume", 0) or 0),          # 手 ✅
                    float(row.get("amount", 0) or 0) / 1000,   # 元→千元
                    0.0))
        if rows:
            logger.info(f"[tickflow] {len(symbols)} stocks: {len(rows)} rows (vol=手✅, amt/1000→千元)")
        return rows

    def _fetch_baostock_turnover(self, symbols: list) -> list:
        """Baostock 逐只获取换手率 — 老牌稳定免费无限制，覆盖1990年至今。

        只返回 (symbol, date, turnover) 用于 UPDATE existing rows。
        不返回 OHLCV（已有其他源负责）。
        """
        try:
            import baostock as bs
        except ImportError:
            raise RuntimeError("baostock not installed (pip install baostock)")
        rows = []
        try:
            bs.login()
            for sym in symbols:
                try:
                    code = f"{'sh' if sym.startswith(('6','9','68')) else 'sz'}.{sym}"
                    rs = bs.query_history_k_data_plus(
                        code, "date,turn",
                        start_date="2020-01-01",
                        end_date=datetime.today().strftime("%Y-%m-%d"),
                        frequency="d", adjustflag="2"
                    )
                    if rs.error_code != "0":
                        continue
                    df = rs.get_data()
                    if df.empty:
                        continue
                    for _, row in df.iterrows():
                        t = float(row["turn"]) if row["turn"] and row["turn"] != "" else 0.0
                        if t > 0:
                            rows.append((sym, str(row["date"]).replace("-", ""), t))
                except Exception:
                    continue
        finally:
            bs.logout()
        return rows

    def _analyze_daily_gaps(self, conn) -> dict:
        """分析日线数据缺口: 每只股票的状态分类。

        Returns: {
            'missing': [sym, ...],      # 完全无日线数据
            'stale_threshold': 250,       # 不足此天数视为需要补数据
        }
        """
        rows = conn.execute("""
            SELECT s.symbol, s.market, COUNT(d.symbol) as days,
                   COALESCE(MIN(d.date), '') as min_d,
                   COALESCE(MAX(d.date), '') as max_d
            FROM stocks s
            LEFT JOIN daily d ON s.symbol = d.symbol
            GROUP BY s.symbol
            ORDER BY days ASC
        """).fetchall()

        missing = [r[0] for r in rows if r[2] == 0]
        stale = [r[0] for r in rows if 0 < r[2] < 250]
        full = [r[0] for r in rows if r[2] >= 250]

        return {
            "missing": missing,      # needs full backfill
            "stale": stale,           # needs more data
            "full": full,             # already sufficient
            "total": len(missing) + len(stale) + len(full),
        }

    def update_daily(self, symbols: list = None,
                     start: str = None) -> int:
        """增量更新日线 — 精准缺口分析 + 多源回退。

        流程:
          1. 分析哪些股票缺少数据（不浪费时间拉已有数据）
          2. zzshare 主源 → tushare(tokened) → 腾讯财经 → akshare 兜底
          3. OHLCV 完成后，Baostock 补充换手率

        symbols: None 表示自动分析缺口并只拉缺失/不足的股票
        返回: 新写入的行数
        """
        if start is None:
            from config.loader import get as cfg
            start = cfg("data.start_date", "20200101")

        conn = self._connect()

        # 1. 精准分析数据缺口
        if symbols is None:
            gaps = self._analyze_daily_gaps(conn)
            target = gaps["missing"] + gaps["stale"]
            logger.info(f"daily gaps: {gaps['total']} total, "
                       f"{len(gaps['missing'])} missing, "
                       f"{len(gaps['stale'])} stale(<250d), "
                       f"{len(gaps['full'])} full — pulling {len(target)}")
            if not target:
                logger.info("daily data complete, nothing to pull")
                return 0
            symbols = sorted(target, key=lambda s: s[:2])  # SH first (tushare benefit)
        else:
            logger.info(f"daily update: {len(symbols)} specified stocks")

        # 2. 初始化 tushare（有 token 时作为备源）
        pro = None
        if self.token:
            try:
                import tushare as ts
                ts.set_token(self.token)
                pro = ts.pro_api()
            except Exception:
                pass

        total_new = 0
        batch_size = 50  # TickFlow batch 效率最高
        sources = {}     # source → count

        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i:i + batch_size]
            # 每只股票独立的 start_date
            batch_maxes = conn.execute(
                f"SELECT symbol, MAX(date) FROM daily WHERE symbol IN ({','.join('?' for _ in chunk)}) GROUP BY symbol",
                chunk
            ).fetchall()
            batch_start_map = {r[0]: r[1] for r in batch_maxes if r[1]}
            batch_start = (min(batch_start_map.values())
                          if batch_start_map else start.replace("-", ""))
            if batch_start < start.replace("-", ""):
                batch_start = start.replace("-", "")

            rows = None
            source = "none"

            # 优先级: TickFlow → zzshare → tushare → akshare → 腾讯
            for src_name, fetch_fn in [
                ("tickflow", lambda: self._fetch_tickflow_daily(chunk, batch_start)),
                ("zzshare",  lambda: self._fetch_zzshare_daily(chunk, batch_start)),
                ("tushare",  lambda: self._fetch_batch_tushare(pro, ",".join(_ts_code(s) for s in chunk), batch_start) if pro else None),
                ("akshare",  lambda: self._fetch_akshare_daily(chunk, batch_start)),
                ("tencent",  lambda: self._fetch_tencent_daily(chunk, batch_start)),
            ]:
                if rows is not None:
                    break
                try:
                    result = fetch_fn()
                    if result:
                        rows = result
                        source = src_name
                except Exception as e:
                    logger.debug(f"[{src_name}] unavailable: {e}")
                    continue

            if rows:
                conn.executemany(
                    """INSERT OR IGNORE INTO daily
                       (symbol,date,open,high,low,close,volume,amount,turnover)
                       VALUES (?,?,?,?,?,?,?,?,?)""", rows
                )
                total_new += len(rows)
                sources[source] = sources.get(source, 0) + 1

            # 每5批打印进度 + 样本日志
            if (i // batch_size) % 5 == 0:
                conn.commit()
                # 取本批第一行做样本验证
                sample = rows[0] if rows else None
                sample_str = ""
                if sample:
                    sample_str = f" | sample: {sample[0]} {sample[1]} V={sample[6]} Amt={sample[7]}"
                logger.info(f"daily [{source}] {min(i+batch_size, len(symbols))}/{len(symbols)} "
                           f"{total_new}新行{sample_str}")

            if source == "tushare" and pro is not None:
                time.sleep(0.4)

        conn.commit()

        # 汇总
        src_summary = ", ".join(f"{k}={v}" for k, v in sorted(sources.items()))
        logger.info(f"daily done: {total_new} rows, {len(symbols)} stocks, sources: {src_summary}")

        # 3. Baostock 换手率回填（独立步骤，不阻塞主流程。耗时与0值行数成正比，量级大时不自动触发）
        # 如需回填: store.backfill_turnover(symbols)
        if False:  # disabled — 512万行时不可行，改为独立方法按需调用
            turnover_fill = 0
            try:
                zero_to = conn.execute(
                    "SELECT COUNT(*) FROM daily WHERE turnover=0 OR turnover IS NULL"
                ).fetchone()[0]
                if zero_to > 0:
                    logger.info(f"turnover missing for {zero_to} rows, backfilling via Baostock...")
                    # 按股票分组，只处理 turnover=0 的
                    need_turnover = [r[0] for r in conn.execute(
                        "SELECT DISTINCT symbol FROM daily WHERE turnover=0 OR turnover IS NULL LIMIT 500"
                    ).fetchall()]
                    for s in need_turnover:
                        try:
                            t_rows = self._fetch_baostock_turnover([s])
                            for sym, date, turn in t_rows:
                                conn.execute(
                                    "UPDATE daily SET turnover=? WHERE symbol=? AND date=? AND (turnover=0 OR turnover IS NULL)",
                                    (turn, sym, date)
                                )
                                turnover_fill += 1
                        except Exception:
                            continue
                    conn.commit()
                    logger.info(f"turnover backfill: {turnover_fill} rows updated via Baostock")
            except Exception as e:
                logger.warning(f"turnover backfill failed: {e}")

        total_rows = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        src_summary = ", ".join(f"{k}={v}" for k, v in sorted(sources.items()))
        logger.info(f"daily done: {total_rows} rows total ({total_new} new, sources: {src_summary})")
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
        result = sync_all(self._connect(), max_pb_fetch=-1)
        logger.info(f"fundamentals: PE={result['pe_count']} PB={result['pb_count']}")
        return result["pe_count"]

    def sync_lhb_data(self, start: str = "20230101") -> int:
        """增量同步龙虎榜数据 → lhb_detail 表。从 akshare 拉取，只写新日期。"""
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not available, skipping LHB sync")
            return 0

        conn = self._connect()
        max_date = conn.execute("SELECT MAX(trade_date) FROM lhb_detail").fetchone()[0]
        daily_max = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]

        end = datetime.today().strftime("%Y%m%d")
        if max_date and daily_max and max_date >= daily_max:
            logger.info(f"lhb up to date ({max_date} >= daily {daily_max}), skipping")
            return 0
        start = max_date if max_date else "20230101"

        logger.info(f"syncing LHB data: {start} → {end}")
        try:
            df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        except Exception as e:
            logger.warning(f"LHB fetch failed: {e}")
            return 0

        if df is None or df.empty:
            logger.info("no new LHB records")
            return 0

        conn = self._connect()
        new_count = 0
        for _, row in df.iterrows():
            try:
                sym = str(row.get("代码", "")).zfill(6)
                if len(sym) != 6:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO lhb_detail
                       (symbol, trade_date, close, change_pct, turnover_rate,
                        net_buy, buy_amt, sell_amt, reason)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sym,
                     str(row.get("上榜日", row.get("trade_date", row.get("日期", ""))))[:10].replace("-", ""),
                     float(row.get("收盘价", 0) or 0),
                     float(row.get("涨跌幅", 0) or 0),
                     float(row.get("换手率", 0) or 0),
                     float(row.get("龙虎榜净买额", 0) or 0),
                     float(row.get("龙虎榜买入额", 0) or 0),
                     float(row.get("龙虎榜卖出额", 0) or 0),
                     str(row.get("上榜原因", "") or "")[:200])
                )
                new_count += 1
            except Exception:
                continue

        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM lhb_detail").fetchone()[0]
        logger.info(f"LHB sync done: {new_count} new, {total} total records")
        return new_count

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
