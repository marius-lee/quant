from datetime import datetime
from quant.config.constants import _require_cfg
from quant.data.cache import get_backend, DataCache, RateLimiter
from quant.utils.baostock_gate import gate as _bs_gate, bs_query as _bs_query, bs_task as _bs_task_deco, BaostockBlacklisted, BaostockQuotaExceeded
"""Mixin class — extracted from DataStore."""

class DataStoreBackfillMixin:
    """Mixin with 4 methods."""

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

        # 不再检测缺口 — INSERT OR IGNORE 确保幂等, 已有数据不重复写
        logger.info(f"backfill_range: {start}→{end} — {len(symbols)} stocks")
        return self._backfill_via_baostock(symbols, start, end)

def _backfill_via_baostock(self, symbols: list, start: str, end: str):
        """baostock 逐只拉取历史 K 线并写入 daily 表 (test-v351)."""
        import baostock as bs
        try:
            _bs_query("login")
        except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
            logger.error(f"baostock backfill aborted: {_e}")
            return
        conn = self._connect()
        total = 0
        for i, sym in enumerate(symbols):
            code = f"sh.{sym}" if sym.startswith(('6','5','9')) else f"sz.{sym}"
            try:
                try:
                    rs = _bs_query(
                        "query_history_k_data_plus",
                        code, 'date,open,high,low,close,volume,amount,turn',
                        start_date=start, end_date=end, frequency='d', adjustflag='2')
                except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                    logger.error(f"baostock backfill {sym}: {_e}; 停止本轮")
                    break
                if rs.error_code != '0':
                    logger.warning(f"baostock backfill {sym}: error_code={rs.error_code} "
                                   f"msg={rs.error_msg} — 该股本轮跳过 (断点续跑可重试)")
                    continue
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    continue
                for r in rows:
                    if r[1] == '' or float(r[1]) == 0:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO daily(symbol,date,open,high,low,close,volume,amount,turnover) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (sym, r[0], float(r[1]), float(r[2]), float(r[3]),
                         float(r[4]), int(float(r[5]) / 100) if r[5] else 0,
                         float(r[6]) / 1000 if r[6] else 0,   # v491: amount 元→千元 (对齐 _fetch_baostock_daily; 原 v407 直写元错 1000 倍)
                         float(r[7]) if r[7] else 0))
                    total += 1
                # v552: 每只立即 commit — 原每 100 只 commit 使 baostock 网络查询
                # (query_history_k_data) 落在 deferred 写事务窗口内 (~100 次查询
                # ≈ 60-120s 长锁, 2026-08-19 backfill 事故同构)
                conn.commit()
                if (i + 1) % 100 == 0:
                    logger.info(f"baostock backfill: {i+1}/{len(symbols)} "
                                f"({(i+1)*100//len(symbols)}%) — {total} rows")
            except Exception as e:
                logger.warning(f"baostock {sym}: {type(e).__name__}: {e}")
        conn.commit()
        bs.logout()
        logger.info(f"baostock backfill done: {len(symbols)} stocks, {total} new rows")
        return total

def backfill_turnover(self, date: str = None, full: bool = False):
        """回填换手率 — baostock 逐只拉取 K 线, 取 turn 字段 UPDATE daily。

        date: 指定时只回填该日; None 时扫描全缺口 (历史存量回填).
        full: True 时按 symbol 全区间拉取 — 2020-2024 全市场 turnover≈0
              (tushare daily API 无此字段, 2026-07-21 实测), 逐日模式需
              7.2M 次查询不可行; 每只 1 次查询, 5208 只 ≈ 40 分钟.
        turn 值与 tushare daily_basic turnover_rate 完全一致 (600519: 0.8492%)。
        来源: scripts/check_turnover_sources.py 实测 + baostock 官方文档。
        """
        if full:
            return self._backfill_turnover_full()
        import socket as _socket
        _socket.setdefaulttimeout(_require_cfg("data.http_timeout.baostock"))  # baostock 阻塞 socket 兜底 (2026-08-13 实测服务器挂起永久阻塞)
        import time as _time
        from datetime import datetime, timedelta
        from quant.execution.calendar import is_trading_day
        conn = self._connect()

        # ── 确定回填日期范围 ──
        if date:
            # 单日模式: 只回填指定日期, 跳过全缺口扫描
            # 排除北交所 (92xxx): baostock 不覆盖北交所, 逐日查询必然失败
            # (来源: 2026-08-13 实测 266 只 daily 全零且 baostock query error),
            # 北交所换手率由 tickflow (backfill_turnover_quotes) 处理.
            needs_fill = {}
            syms = [r[0] for r in conn.execute(
                "SELECT symbol FROM daily WHERE date=? AND (turnover=0 OR turnover IS NULL) "
                "AND symbol NOT LIKE '92%'", (date,)
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
            # 无 date 全量模式: 历史存量缺口 (2020-2024 全市场 turnover≈0,
            # ~720 万行) 无法用逐日模式 (需 7.2M 次 baostock 查询) — 旧实现
            # 从 last_good (MAX turnover>0) 起扫, 历史大洞在 last_good 之前
            # 永远扫不到 (2026-08-13 实测). 直接路由 full 模式 (每 symbol 一次查询).
            logger.info("turnover backfill: no date → full mode (per-symbol full range)")
            return self._backfill_turnover_full()

        _est_sec = total_stocks * 0.3 + total_stocks * 0.15
        logger.info(f"turnover backfill: {total_stocks} stock×dates via baostock, ~{_est_sec/60:.0f}min estimated")

        # ── baostock login ──
        import baostock as _bs
        try:
            _lg = _bs_query("login")
            if _lg.error_code != '0':
                logger.error(f"baostock login failed: {_lg.error_msg}")
                return 0
            logger.info(f"baostock login: {_lg.error_msg}")
            _bs_socket_timeout()
        except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
            logger.error(f"baostock login blocked: {_e}")
            return 0
        except Exception as _e:
            logger.error(f"baostock import/login failed: {_e}")
            return 0

        _bs_t0 = _time.time()
        total_updated = 0
        _bs_processed = 0
        logger.info("turnover backfill: starting, first progress at 50 stocks "
                    "(全局限速 BaostockGate 0.5s/只, 跨进程共享)")
        for d_offset in range((gap_end_dt - gap_start_dt).days + 1):
            d = (gap_start_dt + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            if not is_trading_day(datetime.strptime(d, "%Y-%m-%d").date()):
                continue
            syms = needs_fill.get(d, [])
            if not syms:
                continue

            logger.info(f"turnover backfill {d}: {len(syms)} stocks via baostock")
            updated_today = 0
            gate_blocked = False
            for i, sym in enumerate(syms):
                code = _ts_code(sym)
                # ── baostock 查询, 3次重试 (2026-08-13: error_code≠0 也重试 —
                #    旧实现仅在 Python 异常时重试, session 失效/服务抖动
                #    (如 \"用户未登录\") 静默跳过 → 缺口永远缺) ──
                tv = 0.0
                for _retry in range(3):
                    try:
                        try:
                            _rs = _bs_query(
                                "query_history_k_data_plus",
                                code, "date,turn",
                                start_date=d, end_date=d,
                                frequency="d", adjustflag="2")
                        except BaostockBlacklisted as _bl:
                            logger.error(f"turnover backfill IP 黑名单: {_bl}; 立即停止")
                            gate_blocked = True
                            break
                        except BaostockQuotaExceeded as _q:
                            logger.error(f"turnover backfill 配额: {_q}; 停止本轮")
                            gate_blocked = True
                            break
                        if _rs.error_code == '0':
                            while _rs.next():
                                row = _rs.get_row_data()
                                if row[0] == d:
                                    tv_str = row[1] if len(row) > 1 else ''
                                    tv = float(tv_str) if tv_str and tv_str.strip() else 0.0
                                    break
                            break
                        # 非零 error_code: 退避重试; session 失效重登一次
                        if _retry < 2:
                            if "登录" in _rs.error_msg or any(
                                    _kw in _rs.error_msg for _kw in
                                    ("网络接收", "接收", "网络错误", "socket", "连接", "超时")):
                                _bs.logout()
                                _lg = _bs_query("login")
                                if _lg.error_code != '0':
                                    logger.warning(f"turnover backfill {d}: baostock re-login failed: {_lg.error_msg}")
                                else:
                                    _bs_socket_timeout()
                            _time.sleep(2 * (_retry + 1))  # 退避: 2s/4s
                        else:
                            if _bs_processed < 5:
                                logger.warning(f"turnover backfill {d}: baostock {code} failed — {_rs.error_msg}")
                    except Exception as _e:
                        if _retry < 2:
                            _time.sleep(2 * (_retry + 1))  # 退避: 2s/4s/6s
                        else:
                            logger.warning(f"turnover backfill {d}: baostock {code} failed after 3 retries — {_e}")
                if gate_blocked:
                    break

                if tv > 0:
                    conn.execute("UPDATE daily SET turnover=? WHERE symbol=? AND date=?", (tv, sym, d))
                    updated_today += 1

                _bs_processed += 1
                # 每 5000 只重登, 防止 session 超时导致 Broken pipe (baostock 免费服务 ~1-2h 超时)
                if _bs_processed > 0 and _bs_processed % 200 == 0:
                    logger.info(f"turnover backfill: baostock re-login at {_bs_processed} stocks")
                    _bs.logout()
                    _lg = _bs_query("login")
                    if _lg.error_code != '0':
                        logger.warning(f"baostock re-login failed: {_lg.error_msg}")
                    else:
                        _bs_socket_timeout()
                if _bs_processed % 50 == 0:
                    _elapsed = _time.time() - _bs_t0
                    _rate = _bs_processed / _elapsed if _elapsed > 0 else 0
                    _eta = (total_stocks - _bs_processed) / _rate if _rate > 0 else 0
                    logger.info(f"turnover backfill: {_bs_processed}/{total_stocks} ({100*_bs_processed//total_stocks}%) "
                                f"{_rate:.1f}stocks/s ETA={_eta/60:.0f}min today={updated_today} total={total_updated}")
                # v552: 每只立即 commit — 原每 100 只 commit 使 baostock 查询 + 失败
                # 重试退避 (2s/4s/6s) 落在写事务窗口内 (1-3 分钟长锁); 现在事务
                # 仅覆盖单只纯内存循环, 网络查询全在事务外
                conn.commit()
                # 全局限速已由 _bs_query (BaostockGate) 统一控制 — 不再裸 sleep
            if gate_blocked:
                break

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
