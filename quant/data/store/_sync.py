from datetime import datetime
from quant.config.constants import _require_cfg
from quant.utils.baostock_gate import gate as _bs_gate, bs_query as _bs_query, bs_task as _bs_task_deco, BaostockBlacklisted, BaostockQuotaExceeded
from quant.utils.date import to_compact
from quant.utils.date import to_str
"""Mixin class — extracted from DataStore."""

class DataStoreSyncMixin:
    """Mixin with 5 methods."""

def sync_stock_list(self) -> int:
        """拉取全A股列表。优先 tushare，失败回退 akshare（免费无频率限制）。"""
        self._init_cache_instance()
        conn = self._connect()
        existing = set(
            r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()
        )

        # 1. Cache check — skip API if fresh data in local cache
        cached = self._stock_list_cache.get("symbols")
        if cached is not None and isinstance(cached, list) and len(cached) > 0:
            insert_count = 0
            for item in cached:
                sym = item.get("symbol", item.get("code", ""))
                if not sym or len(str(sym)) != 6:
                    continue
                if sym not in existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                        (sym, item.get("name", ""), item.get("market", ""),
                         to_str(item.get("list_date", ""))))
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
            self._tushare_limiter.wait()
            df = pro.stock_basic(exchange="", list_status="L",
                fields="ts_code,symbol,name,list_date,market")
            if df is not None and not df.empty:
                # cache the raw response
                self._stock_list_cache.set("symbols", df.to_dict(orient="records"))
                for _, row in df.iterrows():
                    sym = row["symbol"]
                    # 2026-08-18: tushare stock_basic.market 返回中文板块名
                    # (主板/创业板/科创板), 与 'SHSE' 比较恒假 → 全部误标 SH.
                    # 改用 code 前缀推导, 与 sync_delisted_stocks 口径一致
                    if sym.startswith(("6", "9", "68")):
                        market = "SH"
                    elif sym.startswith(("4", "8", "92")):
                        market = "BJ"
                    else:
                        market = "SZ"
                    if sym not in existing:
                        conn.execute(
                            "INSERT OR IGNORE INTO stocks(symbol,name,market,list_date) VALUES(?,?,?,?)",
                            (sym, row["name"], market, to_str(row.get("list_date", ""))))
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
                df = ak.stock_info_a_delist()
                if df is None or df.empty:
                    return None
                # v535: 字段名标准化 (上交所接口的中文列名)
                return df.rename(columns={
                    "证券代码": "symbol", "公司代码": "symbol",
                    "证券简称": "name", "公司简称": "name",
                    "终止上市日期": "delist_date", "delisting_date": "delist_date",
                })
            except AttributeError:
                import pandas as _pd2
                df_sh = ak.stock_info_sh_delist()
                df_sz = ak.stock_info_sz_delist()

                def _norm(d, sym_col, name_col, date_col):
                    """v535: 上交所/深交所接口均为中文列名 — 原代码按英文键
                    row.get("symbol") 恒 None → 全部写成 "000000" (INSERT OR
                    IGNORE → 367 "new" 只落 1 条, 退市名单从未真正入库).
                    显式列映射 + 独立构造, 避免 concat 后同名列 row.get
                    返回 Series (symbol 列重复 → len(sym)!=6 全部跳过)."""
                    out = _pd2.DataFrame()
                    out["symbol"] = d[sym_col].astype(str).str.zfill(6)
                    out["name"] = d[name_col]
                    out["delist_date"] = d[date_col]
                    return out

                # SH: 暂停上市日期; SZ: 终止上市日期 (SH 终止列全 NaN)
                _df = _pd2.concat([
                    _norm(df_sh, "公司代码", "公司简称", "暂停上市日期"),
                    _norm(df_sz, "证券代码", "证券简称", "终止上市日期"),
                ], ignore_index=True)
                return _df

        df = _fetch_delist()
        if df is None or df.empty:
            return 0
        new_count = 0
        for _, row in df.iterrows():
            sym = str(row.get("symbol", row.get("code", ""))).zfill(6)
            name = row.get("name", "")
            delist_d = to_str(row.get("delist_date", row.get("delisting_date", "")))
            if len(sym) != 6 or sym in existing:
                continue
            if sym.startswith(("6", "9", "68")):
                mkt = "SH"
            elif sym.startswith(("4", "8", "92")):
                mkt = "BJ"
            else:
                mkt = "SZ"
            # 2026-08-18: REPLACE 整行重建会清空该股 daily_basic 写入的
            # 市值/PE/行业等列 → 存在则 UPDATE, 不存在才 INSERT
            if conn.execute("SELECT 1 FROM stocks WHERE symbol=?", (sym,)).fetchone():
                conn.execute(
                    "UPDATE stocks SET name=?, market=?, list_status='D', delist_date=? WHERE symbol=?",
                    (name, mkt, delist_d, sym))
            else:
                conn.execute(
                    "INSERT INTO stocks(symbol, name, market, list_status, delist_date) "
                    "VALUES(?,?,?,?,?)",
                    (sym, name, mkt, "D", delist_d))
            new_count += 1
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE list_status='D'"
        ).fetchone()[0]
        logger.info(
            f"delisted sync: {new_count} new ({total} total delisted)")

def sync_industry(self):
        """拉取行业分类 — baostock 证监会行业分类 (0.9.20 起支持 Python 3.14; akshare 回退)。

        注意: baostock ≥0.9.20 已实测兼容 Python 3.14 (2026-07-26 纠偏, 旧注释过时)。
        数据已分类时直接跳过。
        """
        self._init_cache_instance()
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
        cached = self._industry_cache.get("mapping")
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
        _bs_gate.acquire()  # 全局限速: 防 IP 拉黑 (2026-08-14)
        bs.login()
        try:
            rs = bs.query_stock_industry()
        except BaostockBlacklisted as _bl:
            logger.error(f"industry sync aborted: {_bl}")
            return 0
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
        self._industry_cache.set("mapping", industry_map)

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
            self._init_cache_instance()  # 初始化 _tushare_limiter

            for bi in range(0, min(len(pending), max_batches * batch_size), batch_size):
                chunk = pending[bi:bi + batch_size]
                codes = self._ts_codes(chunk)
                if not codes:
                    continue
                self._tushare_limiter.wait()
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

        lg = _bs_query("login")
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
                    try:
                        lg = _bs_query("login")
                    except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                        logger.error(f"baostock adj_factor aborted: {_e}")
                        break
                    if lg.error_code != "0":
                        logger.warning(f"baostock re-login failed at {processed}: {lg.error_msg}; "
                                       "continuing with old session")
                    else:
                        _time.sleep(0.5)  # 重登后稍等

                try:
                    rs = _bs_query(
                        "query_adjust_factor",
                        code=bs_code, start_date=start, end_date=end_date)
                except BaostockBlacklisted as _b:
                    logger.error(f"baostock adj_factor IP 黑名单: {_b}; 停止本轮")
                    break
                except BaostockQuotaExceeded as _q:
                    logger.error(f"baostock adj_factor 配额: {_q}; 停止本轮")
                    break
                except Exception as e:
                    logger.warning(f"baostock adj_factor {bs_code}: query failed: {e}")
                    processed += 1
                    continue

                if rs.error_code != "0":
                    processed += 1
                    logger.warning(f"baostock adj_factor {bs_code}: error_code={rs.error_code} "
                                   f"msg={rs.error_msg} — 该股跳过 (缺口由 audit/repair 可见)")
                    continue

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

            # 全局限速已由 _bs_query (BaostockGate) 统一控制 — 不再裸 sleep

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
