from datetime import datetime
from quant.config.constants import _require_cfg
from quant.data.store._helpers import _TICKFLOW_BATCH_NO_PERM
from quant.utils.baostock_gate import gate as _bs_gate, bs_query as _bs_query, bs_task as _bs_task_deco, BaostockBlacklisted, BaostockQuotaExceeded
from quant.utils.date import to_compact
from quant.utils.date import to_str
import pandas as pd
"""Mixin class — extracted from DataStore."""

class DataStoreFetchMixin:
    """Mixin with 10 methods."""

def _fetch_batch_tushare(self, symbols: list, start_date: str) -> list:
        """tushare 批量获取日线 (Token认证, 200call/min). 返回 None 表示不可用。

        fields 必须显式指定 — tushare pro.daily() 默认字段不含 turnover_rate,
        且当前 tushare 版本不传 fields 时返回空 DataFrame。
        start_date 统一转 YYYYMMDD — tushare 不接受 YYYY-MM-DD 格式。
        来源: 2026-07-21 debug_tushare_fields.py 实测
        """
        if not self.token:
            return None
        import tushare as ts
        ts.set_token(self.token)
        pro = ts.pro_api(timeout=_require_cfg("data.http_timeout.tushare"))  # 来源: config.yaml
        ts_codes_parts = []
        for s in symbols:
            if s.startswith("92"):
                ts_codes_parts.append(f"{s}.BJ")
            elif s.startswith(("6", "5", "9")):
                ts_codes_parts.append(f"{s}.SH")
            elif s.startswith(("0", "2", "3")):
                ts_codes_parts.append(f"{s}.SZ")
        if not ts_codes_parts:
            return None
        code_str = ",".join(ts_codes_parts)

        self._init_cache_instance()
        self._tushare_limiter.wait()
        # start_date 统一转 YYYYMMDD — tushare 不接受 YYYY-MM-DD (实测返回空)
        _start = to_compact(start_date)  # 统一转 YYYYMMDD (来源: date.py 策略)
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _call_tushare(code_str, start_date, end_date):
            return pro.daily(
                ts_code=code_str,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )

        df = _call_tushare(code_str, _start, to_compact(datetime.today()))
        if df is None or df.empty:
            return None
        # B-08 fix: tushare daily 返回未复权原始价, 与 tencent/akshare 的 qfq
        # 前复权混写同一张表 → 除权日收益率跳变 (如 -34%), 回测不可复现。
        # B-08 v2: 转 qfq 用本地 adj_factor 表 (sync_adj_factor 后台低频填充),
        # 不再在线调 adj_factor 接口 (限流 1次/小时, 每次 update_daily 都调必然超限)。
        # 本地无因子覆盖的股票跳过 (不写口径不一致数据); 全缺 → None 交给下一源。
        _conn = self._connect()
        _latest_map, _factor_map = self._local_qfq_ratio(_conn, symbols)
        _covered = set(_latest_map)
        if not _covered:
            logger.warning(f"[tushare] no local adj_factor coverage for {len(symbols)} stocks — "
                           f"discarding {len(df)} rows; run sync_adj_factor to backfill")
            return None
        df["symbol6"] = df["ts_code"].str.split(".").str[0]
        df = df[df["symbol6"].isin(_covered)]
        if df.empty:
            return None
        _d_iso = df["trade_date"].str[:4] + "-" + df["trade_date"].str[4:6] + "-" + df["trade_date"].str[6:]
        df["adj_factor"] = [
            _factor_map.get(s, {}).get(d)
            for s, d in zip(df["symbol6"], _d_iso)
        ]
        # 同股票内仅向前填充 (停牌日无因子记录用最近已知因子) —
        # B21 (2026-08-18): 原 ffill().bfill() 用未来因子回填历史行 → 前视.
        # 除权发生在拉取窗口内时, bfill 会把除权后的因子填到除权前价格上,
        # 回测收益失真. 只 ffill, 窗口开头缺失保持 NaN → ratio=1 不复权 (保守).
        df["adj_factor"] = df.groupby("symbol6")["adj_factor"].transform(
            lambda s: s.ffill())
        # 全 None (K线日期与因子日期零重叠) → to_numeric 转 NaN, 防 object/除法 TypeError
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        _ratio = (df["adj_factor"] / df["symbol6"].map(_latest_map)).fillna(1.0)
        for _col in ("open", "high", "low", "close"):
            df[_col] = (df[_col].astype(float) * _ratio).round(4)

        rows = []
        for _, row in df.iterrows():
            rows.append(self._norm_row(
                row["symbol6"], row["trade_date"],
                float(row.get("open", 0)), float(row.get("high", 0)),
                float(row.get("low", 0)), float(row.get("close", 0)),
                float(row.get("vol", 0)), float(row.get("amount", 0)),
                float(0.0)))  # tushare daily API 不含 turnover_rate (来源: 2026-07-21 实测)
        logger.info(f"[tushare] {code_str}: {len(rows)} rows "
                    f"(qfq via local factors, {len(_covered)}/{len(symbols)} covered)")
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
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn",
            })
            data = _json.loads(urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.sina")).read().decode("utf-8"))
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
            import time as _time
            _time.sleep(_require_cfg("data.rate_limit.sina_per_stock_sec"))
        return rows

def _fetch_baostock_daily(self, symbols: list, start_date: str) -> list:
        """baostock (证券宝) 日线: qfq 前复权, vol=股→手, amt=元→千元, turnover✅.

        baostock 免费开源, 无 API key, 无严格限流. 用 query_history_k_data_plus
        接口拉取前复权日线 (adjustflag=2), 数据质量可靠但逐只拉取 (0.3s/只).
        适合作为 tushare/zzshare/pytdx 之后的兜底源.

        baostock 符号格式: sh.6xxxxx/sh.9xxxxx, sz.0xxxxx/sz.2xxxxx/sz.3xxxxx.
        已在 Python 3.14 (baostock ≥0.9.20) 实测兼容.
        来源: 2026-07-30 — akshare IP 封禁, baostock 补位 OHLCV 兜底.
        """
        import time as _time
        try:
            import baostock as bs
        except ImportError:
            return None

        lg = _bs_query("login")
        if lg.error_code != "0":
            logger.warning(f"baostock login failed: {lg.error_msg}")
            return None

        rows = []
        try:
            for i, sym in enumerate(symbols):
                # 6位数代码 → baostock 格式
                if sym.startswith(("6", "9")):
                    bs_code = f"sh.{sym}"
                else:
                    bs_code = f"sz.{sym}"

                try:
                    rs = _bs_query(
                        "query_history_k_data_plus",
                        code=bs_code,
                        fields="date,open,high,low,close,volume,amount,turn",
                        start_date=start_date,
                        end_date=datetime.today().strftime("%Y-%m-%d"),
                        frequency="d",
                        adjustflag="2",  # 2=前复权
                    )
                except BaostockBlacklisted as _b:
                    logger.error(f"baostock daily IP 黑名单: {_b}; 停止本轮")
                    break
                except BaostockQuotaExceeded as _q:
                    logger.error(f"baostock daily 配额: {_q}; 停止本轮")
                    break
                except Exception as e:
                    logger.warning(f"baostock daily {bs_code}: query failed: {e}")
                    continue

                if rs.error_code != "0":
                    # 常见"无数据"类错误码/消息 → DEBUG 级别, 不刷屏
                    # 常见: 日期格式错误(股票未上市/已退市), 无数据, 股票代码不存在
                    no_data_msgs = (
                        "日期格式不正确", "无数据", "不存在", "不在交易日历",
                        "stock not exist", "no data", "invalid date",
                    )
                    msg = rs.error_msg or ""
                    is_no_data = any(m in msg for m in no_data_msgs)
                    level = logger.debug if is_no_data else logger.warning
                    level(f"baostock daily {bs_code}: error_code={rs.error_code} "
                          f"msg={rs.error_msg} — 该源本轮放弃 (后续源兜底)")
                    continue

                while rs.next():
                    row_data = rs.get_row_data()
                    # row_data: [date, open, high, low, close, volume, amount, turn]
                    try:
                        d = row_data[0]
                        o = float(row_data[1])
                        h = float(row_data[2])
                        l = float(row_data[3])
                        c = float(row_data[4])
                        vol = float(row_data[5]) / 100.0  # 股→手
                        amt = float(row_data[6]) / 1000.0  # 元→千元
                        turnover = float(row_data[7]) if row_data[7] else 0.0
                    except (ValueError, IndexError, TypeError):
                        continue
                    rows.append(self._norm_row(sym, d, o, h, l, c, vol, amt, turnover))

            # 全局限速已由 _bs_query (BaostockGate) 统一控制 — 不再裸 sleep
        finally:
            bs.logout()

        return rows if rows else None

def _fetch_tencent_daily(self, symbols: list, start_date: str) -> list:
        """东方财富 K线: vol=手, amt=元→/1000→千元.

        TLS 指纹对抗: 使用 curl_cffi 模拟 Chrome 131, 绕过 eastmoney CDN 的 JA3 检测。
        域名从 82.push2his 迁移到 push2.eastmoney.com (82 子域被定向 DNS 封禁)。
        来源: 2026-07-20 Python requests → RemoteDisconnected, curl 同机正常 → TLS 指纹封禁。
        """
        import curl_cffi.requests as _req, json as _json
        rows = []
        end_date = str(datetime.today().strftime("%Y-%m-%d"))
        _session = _req.Session()
        for sym in symbols:
            code = f"1.{sym}" if sym.startswith("6") else f"0.{sym}"
            try:
                r = _session.get(
                    "https://push2.eastmoney.com/api/qt/stock/kline/get",
                    params={
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                        "ut": "7eea3edcaed734bea9cbfc24409ed989",
                        "klt": "101", "fqt": "1", "secid": code,
                        "beg": to_compact(start_date), "end": to_compact(end_date),
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=_require_cfg("data.http_timeout.tencent"),
                    impersonate="chrome131"
                )
                if r.status_code != 200:
                    continue
                data = r.json().get("data", {})
                klines = data.get("klines")
                if not klines:
                    continue
                for k_str in klines:
                    p = k_str.split(",")
                    d = p[0]
                    if d < start_date:
                        continue
                    rows.append(self._norm_row(
                        sym, d,
                        float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                        float(p[5]),      # vol (手, eastmoney 直接就是手)
                        float(p[6]) / 1000 if len(p) > 6 and p[6] else 0.0,  # amt 元→千元
                       0.0))
            except Exception:
                logger.debug(f"[tencent/em82] {sym} request failed, skipping")
                continue
        if rows:
            logger.info(f"[tencent/em82] {len(symbols)} stocks: {len(rows)} rows")
        return rows

def _fetch_akshare_daily(self, symbols: list, start_date: str) -> list:
        """akshare 逐只日线: vol=手, amt=元 →/1000→千元, 唯一有历史换手率✅

        TLS 指纹对抗: akshare 内部使用 requests 库, 被 eastmoney CDN JA3 检测拦截。
        临时替换 sys.modules['requests'] 为 curl_cffi.requests, 调用后恢复。
        来源: 2026-07-20 python requests → RemoteDisconnected, curl_cffi → HTTP 200。
        """
        import sys
        import requests as _orig_requests
        import curl_cffi.requests as _curl_requests

        self._init_cache_instance()
        self._akshare_limiter.wait()
        try:
            import akshare as ak
        except ImportError:
            raise RuntimeError("akshare not installed")
        rows = []
        end_date = to_compact(datetime.today())  # akshare API只接受YYYYMMDD

        # Monkey-patch: 替换 requests 为 curl_cffi, 绕过 TLS 指纹检测
        sys.modules['requests'] = _curl_requests
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry(delay=3)
        def _fetch_one(sym, s, e):
            # delay=3: akshare(东方财富)默认1s偏激进, 3s给服务器冷却窗口
            return ak.stock_zh_a_hist(symbol=sym, period="daily",
                                      start_date=s, end_date=e, adjust="qfq")

        try:
            for sym in symbols:
                try:
                    df = _fetch_one(sym, to_compact(start_date), end_date)
                except Exception as _e:
                    _retry_tries, _retry_delay = 4, 3
                    logger.warning(f"[akshare] {sym} retry exhausted ({_retry_tries} attempts, delay={_retry_delay}s): {type(_e).__name__}: {_e}")
                    continue
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
                import time; time.sleep(_require_cfg("data.rate_limit.akshare_per_stock_sec"))
        finally:
            sys.modules['requests'] = _orig_requests

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
            ts_code = _ts_code(sym)
            df = api.daily(ts_code=ts_code, start_date=to_compact(start_date), end_date=end_date)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                rows.append(self._norm_row(
                    sym, str(row["trade_date"])[:10],  # _norm_row → to_str() 归一化
                    float(row.get("open", 0) or 0), float(row.get("high", 0) or 0),
                    float(row.get("low", 0) or 0), float(row.get("close", 0) or 0),
                    float(row.get("vol", 0) or 0), float(row.get("amount", 0) or 0), 0.0))
        if rows:
            logger.info(f"[zzshare] {len(symbols)} stocks: {len(rows)} rows (vol=手, amt=千元)")
        return rows

def _fetch_tickflow_daily(self, symbols: list, start_date: str = None) -> list:
        """TickFlow 批量日线: vol=手✅, amt=元❌→/1000→千元。

        历史K线先试注册版 TickFlow(api_key), 无批量K权限/未配置 → 免费层
        (test-v303 权限感知故障转移, 显式日志),
        当天数据用 API key _fetch_tickflow_quotes() 补充。
        来源: tickflow 免费版 "日K为历史数据, 盘中不会实时更新";
              API key 支持 tf.quotes.get() 实时行情含 turnover_rate
        """
        # 当天: 直接用 API key, 跳过免费版 (免费版日K不含当天)
        _tdy = datetime.today().strftime("%Y-%m-%d")
        # to_compact 归一化: 防止 YYYYMMDD vs YYYY-MM-DD 格式不匹配
        if start_date and to_compact(start_date) >= to_compact(_tdy):
            return self._fetch_tickflow_quotes(symbols, start_date)
        try:
            from tickflow import TickFlow
        except ImportError:
            raise RuntimeError("tickflow not installed (pip install tickflow)")
        rows = []
        def _tickflow_code(s):
            if s.startswith('920'): return f"{s}.BJ"       # BSE 北京交易所
            if s.startswith(('6','9','68')): return f"{s}.SH"  # 上海
            return f"{s}.SZ"                               # 深圳
        codes = [_tickflow_code(s) for s in symbols]
        from quant.data.datasource_retry import datasource_retry

        @datasource_retry
        def _call_tickflow_batch(client, codes):
            return client.klines.batch(codes, period="1d", count=10000, as_dataframe=True, show_progress=False)

        # test-v303: 权限感知故障转移 — 先试注册版批量K (api.tickflow.org),
        # PermissionError (套餐无批量K权限, 2026-07-26 实测) → 记 flag 落免费层;
        # 升级套餐后新进程自动走回注册版。注册端单次尝试不过 retry:
        # 权限错误重试 4 次 × 15s 纯属浪费。
        global _TICKFLOW_BATCH_NO_PERM
        dfs = None
        if not _TICKFLOW_BATCH_NO_PERM:
            try:
                _api_key = _require_cfg("data.tickflow_api_key")
            except KeyError:
                _api_key = None
            if _api_key:
                try:
                    dfs = TickFlow(api_key=_api_key).klines.batch(
                        codes, period="1d", count=10000, as_dataframe=True, show_progress=False)
                    logger.info(f"[tickflow] 注册版批量K线 OK ({len(codes)} codes)")
                except Exception as _e:
                    from tickflow import PermissionError as _TFPermissionError
                    if isinstance(_e, _TFPermissionError):
                        _TICKFLOW_BATCH_NO_PERM = True
                    logger.warning(
                        f"[tickflow] 注册版批量K线失败 ({type(_e).__name__}: {_e}) → 免费层")
            else:
                logger.info("[tickflow] data.tickflow_api_key 未配置 → 免费层 (仅历史日K)")
        if dfs is None:
            dfs = _call_tickflow_batch(TickFlow.free(), codes)
        # B-08: tickflow 日K 未复权 → 本地 adj_factor 表转 qfq (同 tushare 口径,
        # test-v304), 不再直接落库混入 qfq 表 — 除权日收益率跳变, 回测不可复现。
        # 无本地因子覆盖的股票跳过 (不写口径不一致数据); 全缺 → None 交下一源。
        _conn = self._connect()
        _latest_map, _factor_map = self._local_qfq_ratio(_conn, symbols)
        _covered = set(_latest_map)
        if not _covered:
            logger.warning("[tickflow] no local adj_factor coverage — run "
                           "sync_adj_factor first; skip raw write, next source")
            return None
        dfs = {c: d for c, d in dfs.items() if c.split(".")[0] in _covered}
        if not dfs:
            return None
        for code, df in dfs.items():
            if df.empty:
                continue
            sym = code.split(".")[0]
            # ratio = factor(date) / latest_factor; 停牌日无因子记录 → 该股内仅向前填充,
            # B21 (2026-08-18): 原 ffill().bfill() 用未来因子回填历史 → 前视;
            # 窗口开头仍缺失则当天不复权 (ratio=1, 保守). df 先按日期排序保证填充方向正确。
            df = df.sort_values("trade_date")
            _fmap = _factor_map.get(sym, {})
            _tds = df["trade_date"].astype(str).str[:10]
            # 归一化 YYYYMMDD → YYYY-MM-DD (与 adj_factor 表键一致)
            _td_iso = _tds.where(
                _tds.str.contains("-"),
                _tds.str[:4] + "-" + _tds.str[4:6] + "-" + _tds.str[6:8])
            df["adj_factor"] = [_fmap.get(d) for d in _td_iso]
            df["adj_factor"] = df["adj_factor"].ffill()
            # 全 None (K线日期与因子日期零重叠) → to_numeric 转 NaN, 防 object/除法 TypeError
            df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
            _ratio = (df["adj_factor"] / _latest_map[sym]).fillna(1.0)
            for _col in ("open", "high", "low", "close"):
                df[_col] = (df[_col].astype(float) * _ratio).round(4)
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
        # 当天数据用 API key 补充 (免费版日K不含当天)
        from datetime import datetime as _dt
        _td = _dt.today().strftime('%Y-%m-%d')
        _qd = [r[1] for r in rows]
        if _td not in _qd:
            _qr = self._fetch_tickflow_quotes(symbols, _td)
            if _qr:
                rows.extend(_qr)
                logger.info(f'[tickflow] +{len(_qr)} today rows from API key quotes')
        if rows:
            logger.info(f"[tickflow] {len(symbols)} stocks: {len(rows)} rows "
                        f"(qfq via local factors, {len(_covered)}/{len(symbols)} covered; "
                        f"vol=手✅, amt/1000→千元)")
        return rows

def _fetch_tickflow_quotes(self, symbols: list, date: str) -> list:
        """TickFlow API key 实时行情 → 日线行格式, 含 turnover_rate。

        当天数据源 — TickFlow.free().klines.batch() 不含当天K线
        ("日K数据为历史数据, 盘中不会实时更新")。
        注册 API key 支持 tf.quotes.get() 实时行情, 含 turnover_rate。
        来源: tickflow.org 注册文档; config data.tickflow_api_key
        """
        from tickflow import TickFlow
        _api_key = _require_cfg("data.tickflow_api_key")
        tf = TickFlow(api_key=_api_key)

        def _tickflow_code(s):
            # 幂等: 已带后缀不再加
            if '.' in s: return s
            if s.startswith('920'): return f"{s}.BJ"
            if s.startswith(('6','9','68')): return f"{s}.SH"
            return f"{s}.SZ"

        codes = [_tickflow_code(s) for s in symbols]

        # tickflow quotes API 单次最大 5 只, 超过分块
        _batch_max = 5
        rows = []
        for _i in range(0, len(codes), _batch_max):
            _chunk = codes[_i:_i + _batch_max]
            from quant.data.datasource_retry import datasource_retry

            @datasource_retry
            def _call_tickflow_quotes(chunk):
                return tf.quotes.get(symbols=chunk, as_dataframe=True)

            try:
                quotes_df = _call_tickflow_quotes(_chunk)
            except Exception as _e:
                logger.warning(f"[tickflow quotes] chunk {_i} retry exhausted (4 attempts, 1-2-4-8s): {_e}")
                continue
            if quotes_df is None or quotes_df.empty:
                continue
            for _, q in quotes_df.iterrows():
                sym = str(q.get("symbol", "")).split(".")[0]
                if not sym:
                    continue
                _turnover = float(q.get("ext.turnover_rate", 0) or 0)

                rows.append(self._norm_row(
                    sym, date,
                    float(q.get("open", 0) or 0),
                    float(q.get("high", 0) or 0),
                    float(q.get("low", 0) or 0),
                    float(q.get("last_price", 0) or 0),
                    float(q.get("volume", 0) or 0),
                    float(q.get("amount", 0) or 0) / 1000,
                    _turnover))

        if rows:
            logger.info(f"[tickflow quotes] {len(symbols)} stocks: {len(rows)} rows (vol+turnover✅)")
        return rows

def _fetch_longbridge_daily(self, symbols: list, start_date: str = None) -> list:
        """Longbridge (longport) 日线 — 前复权, vol=股✅, amt=元✅。

        需要: pip install longport + 配置 LONGPORT_APP_KEY/LONGPORT_APP_SECRET/LONGPORT_ACCESS_TOKEN
        未安装或未配置 → 静默回退下一源。
        免费额度: 日K线 100次/分钟, 每次最多 200 只股票。
        """
        try:
            import longport as _lb
        except ImportError:
            logger.info("[longbridge] longport not installed, skip — pip install longport")
            return []

        app_key = os.environ.get("LONGPORT_APP_KEY")
        app_secret = os.environ.get("LONGPORT_APP_SECRET")
        access_token = os.environ.get("LONGPORT_ACCESS_TOKEN")
        if not all([app_key, app_secret, access_token]):
            logger.info("[longbridge] missing credentials (LONGPORT_APP_KEY/SECRET/TOKEN), skip")
            return []

        try:
            config = _lb.Config(
                app_key=app_key, app_secret=app_secret, access_token=access_token
            )
            ctx = _lb.QuoteContext(config)
        except Exception as e:
            logger.warning(f"[longbridge] connection failed: {e}, skip")
            return []

        rows = []
        try:
            for sym in symbols:
                try:
                    # A股 → longport 格式: 000001.SZ → 000001.SZ
                    resp = ctx.history_candlesticks_by_offset(
                        sym, _lb.Period.Day, _lb.AdjustType.Forward,
                        count=1, end_date=datetime.now()
                    )
                    if resp and len(resp) > 0:
                        c = resp[0]
                        rows.append({
                            "symbol": sym.replace(".SZ", "").replace(".SH", ""),
                            "date": c.timestamp.strftime("%Y-%m-%d"),
                            "open": float(c.open), "high": float(c.high),
                            "low": float(c.low), "close": float(c.close),
                            "volume": int(c.volume), "amount": float(c.volume * (c.high + c.low + c.close) / 3) if c.amount == 0 else float(c.amount),
                            "turnover": None,
                        })
                except Exception as e:
                    logger.warning(f"[longbridge] {sym} query failed: {e}, skip")
                    continue
        finally:
            try:
                ctx.close() if hasattr(ctx, 'close') else None
            except Exception as _e:
                logger.debug("db context close failed (non-fatal): %s", _e)

        logger.info(f"[longbridge] {len(rows)} rows for {len(symbols)} symbols")
        return rows

def _fetch_pytdx_daily(self, symbols: list, start_date: str) -> list:
        """Pytdx (通达信) 日线 + 前复权: vol=手, amt=元→/1000→千元。

        数据源: 通达信 (Tong Da Xin) 标准行情协议 — 国内最老牌的免费行情协议。
        提供商: 财富趋势科技 (已上市, 股票代码 688318), 通达信客户端覆盖绝大多数券商。
        服务器: 180.153.18.170:7709 (TCP 直连, 无需 API key, 无需认证).
        特点: 数据质量可靠、稳定运行 30 年+、无频率限制 (TCP 逐只拉取).
        缺点: 不提供换手率 (turnover=0 回填)、未复权需手算前复权因子、逐只拉取慢.
        Pytdx 返回未复权数据，通过 get_xdxr_info 获取除权除息记录手算前复权因子。

        来源: ③ Pytdx 是国内最老牌的免费行情协议，数据质量可靠。
        """
        try:
            from pytdx.hq import TdxHq_API
        except ImportError:
            raise RuntimeError("pytdx not installed")

        api = TdxHq_API()
        # socket pre-probe: avoid C extension connect() blocking indefinitely
        import socket as _socket
        _connect_timeout = _require_cfg("data.pytdx.connect_timeout")
        _sock = _socket.create_connection(("180.153.18.170", 7709), timeout=_connect_timeout)
        _sock.close()
        if not api.connect('180.153.18.170', 7709):
            logger.warning("pytdx: server unreachable")
            api.disconnect()
        rows = []
        try:
            for sym in symbols:
                # 市场: 0=深圳, 1=上海
                if sym.startswith(('0', '2', '3')):
                    market = 0
                else:
                    market = 1

                # 1. 获取除权除息记录 (用于前复权计算)
                xdxr = api.get_xdxr_info(market, sym)
                adj_map = {}
                if xdxr:
                    events = []
                    for r in xdxr:
                        songzhuan = float(r.get('songzhuangu', 0) or 0)
                        if songzhuan > 0:
                            d = '%d-%02d-%02d' % (r['year'], r['month'], r['day'])
                            events.append((d, 1 + songzhuan / 10))
                    if events:
                        events.sort(key=lambda x: x[0])
                        # cum[i] = product of (1+R) from events[0] to events[i]
                        cum = 1.0
                        for d, ratio in events:
                            cum *= ratio
                            adj_map[d] = cum
                        # Now for a bar date D, factor = 1 / product of events AFTER D
                        # = 1 / (cum_last / cum_at_or_before_D)
                        # Actually simpler: for each bar date, multiply by 1/ratio for each event after it

                # 3. 获取日线
                bars = api.get_security_bars(9, market, sym, 0, 2000)
                if not bars:
                    continue

                # 对每个bar应用前复权
                for b in bars:
                    d = '%d-%02d-%02d' % (b['year'], b['month'], b['day'])
                    if d < start_date:
                        continue

                    o, h, l, c = (float(b['open']), float(b['high']),
                                  float(b['low']), float(b['close']))
                    vol = float(b['vol'])
                    amt = float(b['amount'])

                    # 前复权: 找到日期 >= d 的除权事件，累积复权因子
                    # factor = 1 / product(ratio for event_date > d)
                    factor = 1.0
                    if adj_map:
                        # cum_at_date = product of ratios up to and including d
                        # We need 1 / product of ratios AFTER d
                        cum_before = 1.0
                        cum_all = 1.0
                        found = False
                        for ed, ratio in sorted(adj_map.items()):
                            cum_all = ratio
                            if ed <= d:
                                cum_before = ratio
                                found = True
                        # ratios after d = cum_all / cum_before (if cum_before != 0)
                        # factor for prices at d = 1 / (ratios after d)
                        if found and cum_before > 0:
                            factor = cum_before / cum_all
                        else:
                            factor = 1.0 / cum_all

                    o_adj = round(o * factor, 4)
                    h_adj = round(h * factor, 4)
                    l_adj = round(l * factor, 4)
                    c_adj = round(c * factor, 4)
                    # vol in 手, amt in 元→千元, turnover=0 (pytdx 不提供换手率)
                    rows.append(self._norm_row(sym, d, o_adj, h_adj, l_adj, c_adj, vol, amt / 1000, 0.0))

        finally:
            api.disconnect()

        if rows:
            logger.info(f"[pytdx] {len(symbols)} stocks: {len(rows)} rows (vol=手, amt/1000→千元, qfq manual adj)")
        return rows
