"""SQLite 数据仓库 — 全A股 + 增量更新。
    首次: 下载全部A股列表 + 全部历史日线 → SQLite
    后续: 对比 SQLite 已有数据，只拉取增量日期
"""
import sqlite3
import time
from datetime import datetime
from utils.date import to_str, to_compact, today_str, DEFAULT_START_DATE

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
        # 为基本面因子添加列 (安全迁移, 列已存在时不报错)
        fund_cols = [
            ("pe", "REAL"), ("pb", "REAL"), ("total_mv", "REAL"),
            ("roe", "REAL"), ("high_52w", "REAL"), ("low_52w", "REAL"),
            ("circ_mv", "REAL"), ("eps", "REAL"), ("bvps", "REAL"),
            ("div_yield", "REAL"), ("turnover_rate", "REAL"),
            ("pe_ttm", "REAL"), ("cfps", "REAL"),
        ]
        for col, typ in fund_cols:
            try:
                conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()

    def _connect(self):
        """获取共享连接。check_same_thread=False 允许 Flask 多线程复用。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
        if self.token:
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

    def sync_industry(self):
        """拉取行业分类 — baostock 证监会行业分类 (需 Python ≤3.12; akshare 回退)。

        注意: baostock 当前不支持 Python 3.14。数据已分类时直接跳过。
        """
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
        # baostock attempt
        try:
            import baostock as bs
        except ImportError:
            logger.info("baostock library not installed (no wheel for Python 3.14), trying akshare...")
            return self._sync_industry_akshare(conn)
        try:
            bs.login()
            rs = bs.query_stock_industry()
            df = rs.get_data()
            bs.logout()
            if df.empty:
                return 0
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
        except Exception as e:
            try:
                bs.logout()
            except Exception:
                pass
            logger.warning(f"baostock industry sync failed: {e}, trying akshare...")
            return self._sync_industry_akshare(conn)


    # ============================================================
    # 日线数据 — 增量更新（tushare 优先，失败回退 akshare）
    # ============================================================

    @staticmethod
    def _norm_row(sym: str, date: str, o: float, h: float, l: float, c: float,
                  vol: float, amt: float, turnover: float = 0.0) -> tuple:
        """标准化一行日线数据: 日期→ISO(YYYY-MM-DD), 成交量→手, 成交额→千元, 精度4位小数。"""
        from utils.date import to_str
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

    def _fetch_batch_tushare(self, pro, ts_codes: str, batch_start: str):
        """tushare 批量获取日线: vol=手, amt=千元 ✅ 无需换算"""
        df = pro.daily(
            ts_code=ts_codes,
            start_date=batch_start,
            end_date=to_compact(datetime.today()),  # tushare API只接受YYYYMMDD, 不接受YYYY-MM-DD
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

    def _fetch_sina_daily(self, symbols: list, start_date: str) -> list:
        """新浪日线: 收盘后即用(15:30), 免费无需注册, vol=股→/100→手, amt=元"""
        import urllib.request, json as _json
        rows = []
        for sym in symbols:
            if sym.startswith('920'): code = f"bj{sym}"        # BSE 北京交易所 (来源: Sina API bj前缀)
            elif sym.startswith(('6','9')): code = f"sh{sym}"  # 上海
            else: code = f"sz{sym}"                             # 深圳
            url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&datalen=2000"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn",
                })
                data = _json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
            except Exception:
                continue
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
                    d = to_str(row[0])
                    if to_compact(d) < to_compact(start_date):  # 腾讯API返回格式不定, compact归一化后字符串比较
                        continue
                    c = float(row[2])          # close
                    vol_raw = float(row[5])     # 股
                    amt_raw = c * vol_raw       # 元 (=close×volume)
                    rows.append(self._norm_row(
                        sym, d,  # d 已由 to_str() 归一化为 YYYY-MM-DD
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
        end_date = to_compact(datetime.today())  # akshare API只接受YYYYMMDD
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
                        str(row["日期"]),  # _norm_row → to_str() 自动归一化
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
        end_date = to_compact(datetime.today())  # akshare API只接受YYYYMMDD
        for sym in symbols:
            try:
                ts_code = _ts_code(sym)
                df = api.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    rows.append(self._norm_row(
                        sym, str(row["trade_date"])[:10],  # _norm_row → to_str() 归一化
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
        def _tickflow_code(s):
            if s.startswith('920'): return f"{s}.BJ"       # BSE 北京交易所
            if s.startswith(('6','9','68')): return f"{s}.SH"  # 上海
            return f"{s}.SZ"                               # 深圳
        codes = [_tickflow_code(s) for s in symbols]
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
        if rows:
            logger.info(f"[tickflow] {len(symbols)} stocks: {len(rows)} rows (vol=手✅, amt/1000→千元)")
        return rows

    def backfill_turnover(self, limit: int = 0):
        """akshare 回填换手率 — 逐只下载日线，只更新 turnover=0/NULL 的行。

        baostock 在 Python 3.14 不可用，改用 akshare。
        limit=0 表示全部（很慢, ~5000 stocks × 1.5s each ≈ 2h）。
        建议: limit=100 测试，增量同步会自然填充新数据。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not installed — turnover backfill skipped")
            return 0
        from datetime import datetime
        conn = self._connect()
        sql = "SELECT DISTINCT symbol FROM daily WHERE turnover=0 OR turnover IS NULL"
        if limit > 0:
            sql += f" LIMIT {limit}"
        symbols = [r[0] for r in conn.execute(sql).fetchall()]
        if not symbols:
            logger.info("turnover backfill: no missing data")
            return 0

        logger.info(f"turnover backfill: {len(symbols)} stocks via akshare (~{len(symbols)*1.5:.0f}s estimated)")
        filled = 0
        end_date = datetime.today().strftime("%Y%m%d")
        for sym in symbols:
            try:
                df = ak.stock_zh_a_hist(
                    symbol=sym, period="daily",
                    start_date="2020-01-01", end_date=end_date, adjust="")
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    t = float(row.get("换手率", 0) or 0)
                    if t > 0:
                        d = str(row["日期"])[:10]
                        conn.execute(
                            "UPDATE daily SET turnover=? WHERE symbol=? AND date=? AND (turnover=0 OR turnover IS NULL)",
                            (round(t, 4), sym, d)
                        )
                        filled += 1
                import time; time.sleep(1.5)
            except Exception:
                continue
        conn.commit()
        logger.info(f"turnover backfill (akshare): {filled} rows updated for {len(symbols)} stocks")
        return filled

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
        try:
            missing = [r[0] for r in conn.execute(
                "SELECT symbol FROM stocks WHERE industry IS NULL"
            ).fetchall()]
            if not missing:
                logger.info("industry sync: no unclassified stocks")
                return 0
            logger.info(f"industry sync: {len(missing)} unclassified stocks via akshare individual")
            import time
            updated = 0
            for idx, sym in enumerate(missing):
                try:
                    info = ak.stock_individual_info_em(symbol=sym)
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
                except Exception as e:
                    if idx < 3:
                        logger.info(f"stock {sym} industry query failed: {e}")
                    continue
                time.sleep(0.8)  # akshare rate limit
            conn.commit()
            classified = conn.execute(
                "SELECT COUNT(*) FROM stocks WHERE industry IS NOT NULL"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
            logger.info(f"industry sync (akshare individual): {updated} updates, {classified}/{total}")
            return updated
        except Exception as e:
            logger.warning(f"akshare industry sync failed: {e}")
            return 0

    def _analyze_daily_gaps(self, conn) -> dict:
        """分析日线数据缺口: 每只股票的状态分类 (增量版 — PK 覆盖索引)。"""
        from datetime import date, timedelta, datetime
        from utils.date import to_str
        cutoff = to_str(date.today() - timedelta(days=2))
        stale_days = 250

        # 单次查询: PK(symbol,date) 覆盖索引, GROUP BY symbol 只取首尾
        rows = conn.execute("""
            SELECT symbol, MIN(date), MAX(date)
            FROM daily GROUP BY symbol ORDER BY symbol
        """).fetchall()

        # 所有 stocks 符号
        all_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()}
        have_data = set()

        stale, full = [], []
        for sym, min_d, max_d in rows:
            have_data.add(sym)
            if max_d < cutoff:
                stale.append(sym)
                continue
            try:
                d1 = datetime.strptime(min_d, "%Y-%m-%d")
                d2 = datetime.strptime(max_d, "%Y-%m-%d")
                est_trading = int((d2 - d1).days * 0.7)
            except Exception:
                est_trading = 0
            if est_trading < stale_days:
                stale.append(sym)
            else:
                full.append(sym)

        missing = sorted(all_symbols - have_data)

        return {
            "missing": missing, "stale": stale, "full": full,
            "total": len(all_symbols),
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
            start = cfg("data.start_date", DEFAULT_START_DATE)

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
            # 来源: to_compact 归一化为8位数字串, 确保字符串比较正确
            batch_start = (min(batch_start_map.values())
                          if batch_start_map else to_compact(start))
            if to_compact(batch_start) < to_compact(start):
                batch_start = start  # 保持 YYYY-MM-DD 给后续 API 用

            rows = None
            source = "none"

            # 动态轮转: 记录每条每秒速度, 最快的排前面, 失败排最后
            if not hasattr(self, '_source_speed'):
                self._source_speed = {}
            all_sources = [
                ("sina",     lambda: self._fetch_sina_daily(chunk, batch_start)),
                ("tencent",  lambda: self._fetch_tencent_daily(chunk, batch_start)),
                ("akshare",  lambda: self._fetch_akshare_daily(chunk, batch_start)),
            ]
            ordered = sorted(all_sources, key=lambda x: self._source_speed.get(x[0], 999), reverse=True)
            for src_name, fetch_fn in ordered:
                if rows is not None:
                    break
                try:
                    t0 = __import__('time').time()
                    result = fetch_fn()
                    elapsed = __import__('time').time() - t0
                    if result:
                        rows = result
                        source = src_name
                        rps = len(result) / max(elapsed, 0.001)
                        # 指数移动平均: 70%旧+30%新, 防单次波动
                        old = self._source_speed.get(src_name, rps)
                        self._source_speed[src_name] = old * 0.7 + rps * 0.3
                except Exception:
                    self._source_speed[src_name] = -1  # 失败排最后
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

        total_rows = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        src_summary = ", ".join(f"{k}:{v}" for k, v in sources.items() if v > 0) if sources else "none"
        logger.info(f"daily done: {total_rows} rows total ({total_new} new, sources: {src_summary})")
        return total_new

    # ============================================================
    # 读取数据
    # ============================================================

    def get_daily(self, symbols: list, start: str = DEFAULT_START_DATE,
                  end: str = None) -> pd.DataFrame:
        """从 SQLite 读取日线，返回 (dates × stocks) 宽表 DataFrame。
        自动分块避免 SQLite 的 999 参数上限。"""
        # 来源: SQLite SQLITE_MAX_VARIABLE_NUMBER=999, 900+99(date params)=999
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

    def _get_daily_chunk(self, symbols: list, start: str = DEFAULT_START_DATE,
                          end: str = None) -> pd.DataFrame:
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
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        result = df.pivot(index="date", columns="symbol", values=[
            "open", "high", "low", "close", "volume", "amount", "turnover"
        ])
        return result.ffill()  # 停牌日填前一日价格，NaN 不进管线

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
        try:
            from data.fundamental import sync_all
            result = sync_all(self._connect(), max_pb_fetch=-1)
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
        daily_max = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if max_date and daily_max and (max_date or "") >= (daily_max or ""):
            logger.info(f"lhb up to date ({max_date} >= {daily_max}), skipping")
            return 0
        # akshare API 要求 YYYYMMDD 格式 — 仅此处转换
        start = to_compact(max_date) if max_date else to_compact(DEFAULT_START_DATE)
        end = to_compact(datetime.today())

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
                     to_str(row.get("上榜日", row.get("trade_date", row.get("日期", "")))),
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
        """拉取基准指数日线，返回 (date → return) Series (小数, 非百分比)。

        优先从本地 benchmark.db 读取 (通过 data/benchmark.py sync)。
        """
        if start is None:
            from config.loader import get as cfg
            start = cfg("backtest.benchmark_start_date", "2020-01-01")
        # 先尝试本地 benchmark.db (由 scripts/init_data.py --benchmark 同步)
        import sqlite3, os
        bm_db = os.path.join(os.path.dirname(__file__), "benchmark.db")
        if os.path.exists(bm_db):
            try:
                conn = sqlite3.connect(bm_db)
                df = pd.read_sql_query(
                    "SELECT date, close FROM benchmark_daily WHERE index_code=? AND date>=? ORDER BY date",
                    conn, params=(code, start)
                )
                conn.close()
                if not df.empty:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")["close"]
                    return df.pct_change().dropna()
            except Exception as e:
                logger.debug(f"benchmark local read failed: {e}")
        # 回退: akshare 实时拉取
        try:
            from data.benchmark import get_benchmark_returns
            # get_benchmark_returns 返回百分比, 转小数
            bm_pct = get_benchmark_returns(code, start=start)
            if bm_pct.empty:
                return pd.Series(dtype=float, name=code)
            return bm_pct / 100.0
        except Exception as e:
            logger.warning(f"benchmark {code} fetch failed: {e}")
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

    def get_fundamentals(self, symbols: list = None, date: str = None) -> pd.DataFrame:
        """读取基本面数据: PE, PB, 总市值, ROE, 行业, 52周高点, 最新收盘价。

        symbols: 股票列表, None = 全部
        date: 交易日期, 用于获取当日最新收盘价(high52w_dist 因子需要)
        返回: DataFrame(index=symbol, columns=[pe,pb,total_mv,roe,industry,high_52w,close_latest])
        """
        conn = self._connect()
        base_cols = "symbol, pe, pb, total_mv, roe, industry, high_52w, eps, bvps"
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql_query(
                f"SELECT {base_cols} FROM stocks WHERE symbol IN ({placeholders})",
                conn, params=symbols)
        else:
            df = pd.read_sql_query(
                f"SELECT {base_cols} FROM stocks", conn)
        df = df.set_index("symbol")
        # 过滤负值PE/PB
        df.loc[df["pe"] <= 0, "pe"] = None
        df.loc[df["pb"] <= 0, "pb"] = None
        # 加入最新收盘价 (从 daily 表取指定日期的 close)
        if date:
            df_date = pd.read_sql_query(
                "SELECT symbol, close FROM daily WHERE date=?", conn, params=(date,))
            df_date = df_date.set_index("symbol").rename(columns={"close": "close_latest"})
            df = df.join(df_date, how="left")
        else:
            df["close_latest"] = None
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
