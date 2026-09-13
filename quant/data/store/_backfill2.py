from datetime import datetime
from quant.config.constants import _require_cfg
from quant.data.cache import get_backend, DataCache, RateLimiter
from quant.utils.baostock_gate import gate as _bs_gate, bs_query as _bs_query, bs_task as _bs_task_deco, BaostockBlacklisted, BaostockQuotaExceeded
from quant.utils.date import to_compact
import pandas as pd
"""Mixin class — extracted from DataStore."""

class DataStoreBackfill2Mixin:
    """Mixin with 5 methods."""

def _backfill_turnover_full(self) -> int:
        """存量 turnover 全量模式 — 每 symbol 一次 baostock 查询拉全区间, 只更新差异行.

        背景: _fetch_tushare_daily 写 daily.turnover 恒为 0 (tushare daily API
        不含 turnover_rate, 2026-07-21 实测), 2020-2024 全市场 ~99.9% 行
        turnover=0 → 10 个 turnover 系因子在受影响日期物化不出数据且每次全量
        回填都重算 (永不收敛). 逐日模式 (backfill_turnover) 需 7.2M 次查询
        不可行; 本方法每只 1 次查询 5208 只 ≈ 40 分钟.

        复用成熟模式: 每 200 只重登防 session 超时 (同 _sync_adj_factor_baostock),
        session 失效 ("用户未登录") 自动 logout+login 再重试 (2026-08-13:
        20:48 起跑 9 分钟即遇 session 失效, 旧实现 3 次重试全败后白等 200 只),
        config rate_limit.baostock_per_stock_sec 限速, 每 100 只 commit + 进度日志.

        断点续跑 (2026-08-14): 全程进度文件记录已完成的 symbols,
        中断后从断点继续而非重头 (旧实现 5 次起跑全中断于前排 ~700 只,
        后续 ~4600 只永远轮不到 — 2026-08-14 DB 审计发现).
        """
        import time as _time
        import datetime
        import json as _json
        import socket as _socket
        from pathlib import Path as _Path
        _socket.setdefaulttimeout(_require_cfg("data.http_timeout.baostock"))  # baostock 阻塞 socket 兜底
        from quant.data.repos.universe_repo import UniverseRepo

        _progress_path = _Path(__file__).resolve().parents[1] / "data" / ".turnover_full_progress.json"

        def _load_done() -> set:
            if not _progress_path.exists():
                return set()
            try:
                with open(_progress_path, encoding="utf-8") as _f:
                    return set(_json.load(_f).get("done_symbols", []))
            except Exception as _e:
                logger.warning(f"turnover backfill full: 进度文件损坏, 从头跑: {_e}")
                return set()

        def _save_done(done: set) -> None:
            _tmp = _progress_path.with_suffix(".tmp")
            try:
                with open(_tmp, "w", encoding="utf-8") as _f:
                    _json.dump({"done_symbols": sorted(done)}, _f)
                _tmp.replace(_progress_path)
            except Exception as _e:
                raise RuntimeError(f"turnover backfill full: 进度文件写入失败: {_e}")

        conn = self._connect()
        import baostock as _bs
        try:
            _lg = _bs_query("login")
        except BaostockBlacklisted as _bl:
            raise RuntimeError(f"baostock IP 拉黑 (冷却期): {_bl}")
        except BaostockQuotaExceeded as _q:
            raise RuntimeError(f"baostock 配额: {_q}")
        if _lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {_lg.error_msg}")
        _bs_socket_timeout()

        symbols = UniverseRepo().get_symbols(exclude_market="BJ")
        _BS_START = "2018-01-01"  # 覆盖因子 250d lookback 窗口
        logger.info(f"turnover backfill full: {len(symbols)} symbols, "
                    f"start={_BS_START}, ~{len(symbols)*0.55/60:.0f}min estimated")

        def _fetch_turn(code: str) -> dict[str, float]:
            """baostock 查询 date,turn 全区间; session 失效自动重登, 最多 3 次."""
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    try:
                        rs = _bs_query(
                            "query_history_k_data_plus",
                            code, "date,turn",
                            start_date=_BS_START,
                            end_date=datetime.date.today().strftime("%Y-%m-%d"),
                            frequency="d", adjustflag="2")
                    except BaostockBlacklisted as _bl:
                        _bs_gate.mark_blacklisted(str(_bl))
                        raise RuntimeError(f"baostock IP 黑名单: {_bl}")
                    except BaostockQuotaExceeded as _q:
                        raise RuntimeError(f"baostock 配额: {_q}")
                except Exception as _e:  # socket/网络级异常
                    last_err = _e
                    _time.sleep(1.5 * (_attempt + 1))
                    continue
                if rs.error_code == "0":
                    out: dict[str, float] = {}
                    while rs.next():
                        row = rs.get_row_data()
                        if not row or row[1] in ("", "None"):
                            continue
                        try:
                            out[row[0]] = float(row[1])
                        except ValueError:
                            continue
                    return out
                if "登录" in rs.error_msg:  # session 失效 → 重登
                    _bs.logout()
                    try:
                        _lg2 = _bs_query("login")
                    except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                        raise RuntimeError(f"baostock re-login blocked: {_e}")
                    if _lg2.error_code != "0":
                        raise RuntimeError(f"baostock re-login failed: {_lg2.error_msg}")
                    _bs_socket_timeout()
                elif any(_kw in rs.error_msg for _kw in
                         ("网络接收", "接收", "网络错误", "socket", "连接", "超时")):
                    # v489: 服务端断连 — 与 session 失效同层处理, 断连必须重建连接
                    # (2026-08-14: 09:10 起 Broken pipe/Connection reset 连发,
                    #  旧逻辑仅在"登录"关键词时重登, 断连重试 3 次全败)
                    _bs.logout()
                    try:
                        _lg3 = _bs_query("login")
                    except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                        raise RuntimeError(f"baostock re-login blocked: {_e}")
                    if _lg3.error_code != "0":
                        raise RuntimeError(f"baostock re-login failed: {_lg3.error_msg}")
                    _bs_socket_timeout()
                    last_err = RuntimeError(f"baostock {code}: 断连已重登: {rs.error_msg}")
                else:
                    last_err = RuntimeError(f"baostock {code}: {rs.error_msg}")
                _time.sleep(1.5 * (_attempt + 1))
            raise last_err if last_err else RuntimeError(f"baostock {code}: query failed")

        _t0 = _time.time()
        total_upd = 0
        total_same = 0
        failed = 0
        skipped = 0
        done = _load_done()
        _resumed = bool(done)
        for i, sym in enumerate(symbols):
            if sym in done:
                skipped += 1  # 断点续跑: 上次已完成, 跳过
                continue
            code = _ts_code(sym)
            try:
                turns = _fetch_turn(code)
            except Exception as e:
                failed += 1
                logger.warning(f"backfill_turnover full: {sym} failed: {e}")
                if isinstance(e, (BaostockBlacklisted,)) or "黑名单" in str(e) \
                        or "配额" in str(e) or "blocked" in str(e).lower():
                    logger.error(f"backfill_turnover full: 数据源熔断, 立即停止 (已有 {total_upd} 行落库, 进度已存断点)")
                    break
                continue

            existing = dict(conn.execute(
                "SELECT date, turnover FROM daily WHERE symbol=?", (sym,)).fetchall())
            upd = []
            for d, t in turns.items():
                if d not in existing:
                    continue  # daily 表无此行 (停牌/未同步), 不写
                old = existing[d]
                if old is None or abs(old - t) > 1e-9:
                    upd.append((t, d))
                else:
                    total_same += 1
            if upd:
                conn.executemany(
                    "UPDATE daily SET turnover=? WHERE symbol=? AND date=?",
                    [(t, sym, d) for t, d in upd])
                conn.commit()
            total_upd += len(upd)
            done.add(sym)

            # 每 200 只重登, 防 session 超时 (baostock 免费服务 ~1-2h, 同 _sync_adj_factor_baostock)
            if (i + 1) % 200 == 0:
                logger.info(f"backfill_turnover full: re-login at {i+1} stocks")
                _bs.logout()
                try:
                    _lg = _bs_query("login")
                except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                    logger.error(f"backfill_turnover full: re-login blocked: {_e}; 停止")
                    break
                if _lg.error_code != "0":
                    logger.warning(f"baostock re-login failed: {_lg.error_msg}")
            if (i + 1) % 100 == 0:
                _save_done(done)  # 断点: 每 100 只落盘, 中断可续
                _el = _time.time() - _t0
                _rate = (i + 1) / _el
                _eta = (len(symbols) - i - 1) / _rate / 60
                logger.info(f"backfill_turnover full: {i+1}/{len(symbols)} "
                            f"updated={total_upd} same={total_same} "
                            f"skipped={skipped} ({_rate:.1f}/s ETA={_eta:.0f}min)")

        _save_done(done)  # 最终落盘
        _bs.logout()
        conn.close()
        logger.info(f"backfill_turnover full: done — updated={total_upd} "
                    f"same={total_same} failed={failed} skipped={skipped}"
                    f"{' (resumed from checkpoint)' if _resumed else ''}")
        return total_upd

def _backfill_amount_full(self) -> int:
        """存量 daily.amount 全量回填 — 每 symbol 一次 baostock 查询, 只补缺失行.

        背景: 2019 年 daily 数据来自早期源, amount 缺失 ~89% (750k 行, 2026-08-14
        verify 全量检查发现: 2019 仅 10.5% 行有 amount, 2020+ 仅停牌日缺).
        复权/单位口径: baostock 返回 元+股, DB 存 千元+手 (000070 2020-01-02:
        DB amount=261343.875千元 ↔ baostock 261343875.90元, volume=234640手
        ↔ 23464004股 实测一致). 只 UPDATE amount IS NULL 或 =0 的行;
        其他列 (close/volume/turnover) 不动.

        复用 _backfill_turnover_full 模式: 断点文件 + 每 200 只重登 + 限速.
        """
        import time as _time
        import datetime
        import json as _json
        import socket as _socket
        from pathlib import Path as _Path
        _socket.setdefaulttimeout(_require_cfg("data.http_timeout.baostock"))
        from quant.data.repos.universe_repo import UniverseRepo

        _progress_path = _Path(__file__).resolve().parents[1] / "data" / ".amount_full_progress.json"

        def _load_done() -> set:
            if not _progress_path.exists():
                return set()
            try:
                with open(_progress_path, encoding="utf-8") as _f:
                    return set(_json.load(_f).get("done_symbols", []))
            except Exception as _e:
                logger.warning(f"amount backfill full: 进度文件损坏, 从头跑: {_e}")
                return set()

        def _save_done(done: set) -> None:
            _tmp = _progress_path.with_suffix(".tmp")
            try:
                with open(_tmp, "w", encoding="utf-8") as _f:
                    _json.dump({"done_symbols": sorted(done)}, _f)
                _tmp.replace(_progress_path)
            except Exception as _e:
                raise RuntimeError(f"amount backfill full: 进度文件写入失败: {_e}")

        conn = self._connect()
        import baostock as _bs
        try:
            _lg = _bs_query("login")
        except BaostockBlacklisted as _bl:
            raise RuntimeError(f"baostock IP 拉黑 (冷却期): {_bl}")
        except BaostockQuotaExceeded as _q:
            raise RuntimeError(f"baostock 配额: {_q}")
        if _lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {_lg.error_msg}")
        _bs_socket_timeout()

        symbols = UniverseRepo().get_symbols(exclude_market="BJ")
        _BS_START = "2018-01-01"
        logger.info(f"amount backfill full: {len(symbols)} symbols, "
                    f"start={_BS_START}, ~{len(symbols)*0.55/60:.0f}min estimated")

        def _fetch_amount(code: str) -> dict[str, float]:
            """baostock 查询 date,amount 全区间; session 失效自动重登, 最多 3 次."""
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    try:
                        rs = _bs_query(
                            "query_history_k_data_plus",
                            code, "date,amount",
                            start_date=_BS_START,
                            end_date=datetime.date.today().strftime("%Y-%m-%d"),
                            frequency="d", adjustflag="2")
                    except BaostockBlacklisted as _bl:
                        _bs_gate.mark_blacklisted(str(_bl))
                        raise RuntimeError(f"baostock IP 黑名单: {_bl}")
                    except BaostockQuotaExceeded as _q:
                        raise RuntimeError(f"baostock 配额: {_q}")
                except Exception as _e:
                    last_err = _e
                    _time.sleep(1.5 * (_attempt + 1))
                    continue
                if rs.error_code == "0":
                    out: dict[str, float] = {}
                    while rs.next():
                        row = rs.get_row_data()
                        if not row or row[1] in ("", "None"):
                            continue
                        try:
                            # 单位: baostock 元 → DB 千元 (000070 实测一致)
                            out[row[0]] = float(row[1]) / 1000.0
                        except ValueError:
                            continue
                    return out
                if "登录" in rs.error_msg:
                    _bs.logout()
                    try:
                        _lg2 = _bs_query("login")
                    except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                        raise RuntimeError(f"baostock re-login blocked: {_e}")
                    if _lg2.error_code != "0":
                        raise RuntimeError(f"baostock re-login failed: {_lg2.error_msg}")
                    _bs_socket_timeout()
                elif any(_kw in rs.error_msg for _kw in
                         ("网络接收", "接收", "网络错误", "socket", "连接", "超时")):
                    _bs.logout()
                    try:
                        _lg3 = _bs_query("login")
                    except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                        raise RuntimeError(f"baostock re-login blocked: {_e}")
                    if _lg3.error_code != "0":
                        raise RuntimeError(f"baostock re-login failed: {_lg3.error_msg}")
                    _bs_socket_timeout()
                    last_err = RuntimeError(f"baostock {code}: 断连已重登: {rs.error_msg}")
                else:
                    last_err = RuntimeError(f"baostock {code}: {rs.error_msg}")
                _time.sleep(1.5 * (_attempt + 1))
            raise last_err if last_err else RuntimeError(f"baostock {code}: query failed")

        _t0 = _time.time()
        total_upd = 0
        total_same = 0
        failed = 0
        skipped = 0
        done = _load_done()
        _resumed = bool(done)
        for i, sym in enumerate(symbols):
            if sym in done:
                skipped += 1
                continue
            code = _ts_code(sym)
            try:
                amounts = _fetch_amount(code)
            except Exception as e:
                failed += 1
                logger.warning(f"backfill_amount full: {sym} failed: {e}")
                if isinstance(e, (BaostockBlacklisted,)) or "黑名单" in str(e) \
                        or "配额" in str(e) or "blocked" in str(e).lower():
                    logger.error(f"backfill_amount full: 数据源熔断, 立即停止 "
                                 f"(已有 {total_upd} 行落库, 进度已存断点)")
                    break
                continue

            existing = dict(conn.execute(
                "SELECT date, amount FROM daily WHERE symbol=?", (sym,)).fetchall())
            upd = []
            for d, a in amounts.items():
                if d not in existing:
                    continue
                old = existing[d]
                if old is None or abs(old) < 1e-9 or abs(old - a) > 1e-9:
                    upd.append((a, d))
                else:
                    total_same += 1
            if upd:
                conn.executemany(
                    "UPDATE daily SET amount=? WHERE symbol=? AND date=?",
                    [(a, sym, d) for a, d in upd])
                conn.commit()
            total_upd += len(upd)
            done.add(sym)

            if (i + 1) % 200 == 0:
                logger.info(f"backfill_amount full: re-login at {i+1} stocks")
                _bs.logout()
                try:
                    _lg = _bs_query("login")
                except (BaostockBlacklisted, BaostockQuotaExceeded) as _e:
                    logger.error(f"backfill_amount full: re-login blocked: {_e}; 停止")
                    break
                if _lg.error_code != "0":
                    logger.warning(f"baostock re-login failed: {_lg.error_msg}")
            if (i + 1) % 100 == 0:
                _save_done(done)
                _el = _time.time() - _t0
                _rate = (i + 1) / _el
                _eta = (len(symbols) - i - 1) / _rate / 60
                logger.info(f"backfill_amount full: {i+1}/{len(symbols)} "
                            f"updated={total_upd} same={total_same} "
                            f"skipped={skipped} ({_rate:.1f}/s ETA={_eta:.0f}min)")

        _save_done(done)
        _bs.logout()
        conn.close()
        logger.info(f"backfill_amount full: done — updated={total_upd} "
                    f"same={total_same} failed={failed} skipped={skipped}"
                    f"{' (resumed from checkpoint)' if _resumed else ''}")
        return total_upd

def backfill_amount(self, date: str = None, full: bool = False) -> int:
        """回填 daily.amount — baostock 逐只拉取, 只补缺失行 (v490).

        背景: 2019 年 amount 缺失 ~89% (750k 行, 早期源未写该列), verify
        物化阻断. 与 backfill_turnover 同模式: full 每 symbol 一次查询全区间.
        """
        if full or date is None:
            return self._backfill_amount_full()
        conn = self._connect()
        missing = conn.execute(
            "SELECT symbol FROM daily WHERE date=? AND (amount IS NULL OR amount=0)",
            (date,)).fetchall()
        conn.close()
        if not missing:
            return 0
        raise NotImplementedError(
            "backfill_amount 逐日模式未实现 — 存量缺口规模下请用 full 模式")

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
                # v552: 每只立即 commit — 原最后一次性 commit 使 akshare 网络
                # 请求 + sleep 落在 deferred 写事务窗口内 (~317 只 ≈ 5-20 分钟长锁)
                conn.commit()
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
          3. OHLCV 完成后，Baostock 补充换手率 (nightly backfill_turnover)

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
            # 各源简介 (turnover 实际值 2026-07-21/08-13 实测):
            #   - tushare:     批量50股, qfq✅, turnover✗ (daily API 无此字段 → 写 0, 由 backfill_turnover 后补).
            #   - zzshare:     逐只拉取, 前复权, turnover=0.
            #   - pytdx:       通达信 (财富趋势 688318) — TCP 直连, 无需API key, 30年+稳定.
            #   - baostock:    证券宝 — 免费开源, qfq 前复权, turnover✅. 逐只 0.3s, 兜底可靠.
            #   - tencent:     EM K线, qfq 前复权, IP 当前封禁. 等解封后自动恢复.
            #   - akshare:     逐只拉取, EM qfq 前复权, turnover✅. IP 封禁中 → 置后减少白等.
            #   - tickflow:    批量拉取, adj_factor 转 qfq, 无 turnover. 免费版无批量权限.
            #   - longbridge:  批量拉取, 无 turnover. 需凭证.
            # akshare 排在 zzshare/pytdx/tencent 之后: IP 封禁期内减少无效重试 (4次×3s=12s/批).
            # 设计决策: 速度优先 — tushare 首位 99%+ 成功率; turnover 完整性由
            # nightly backfill_turnover 补齐 (2026-08-13: 2020-2024 历史存量
            # 由 backfill_turnover(full) 一次性回填).
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
            # v408: 数据源开关 — tencent/akshare IP封禁中, 跳过节省回退耗时
            # v493: 去掉 try/except — 配置缺键应崩 (零 fallback), 不静默当启用
            _disabled = set()
            for _sn in ("tencent", "akshare"):
                if not _require_cfg(f"data.source_policy.enabled.{_sn}"):
                    _disabled.add(_sn)
            for src_name, fetch_fn in ordered:
                if src_name in _disabled:
                    continue
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
