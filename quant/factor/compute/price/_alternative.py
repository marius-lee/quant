"""价量因子子模块。"""

import traceback

import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional

from quant.utils.date import to_str
from quant.config.constants import *
from quant.factor.registry import _cs_zscore, _db_connect, _FIN_FACTORS, _shared_limit_conn
from quant.factor.compute._shared import _market_db_path

from quant.utils.logger import get_logger as _get_logger
from quant.data.repos._base import DatabaseManager

_log = _get_logger("factor.compute")

# ── ztd 预计算缓存: 消除每交易日重复 SQLite 查询 ──
# key: date_str → value: Series(index=symbol, value=ztd_ratio)
_ztd_cache: dict = {}


def preload_ztd_cache(dates: list, all_symbols: list):
    """一次性预计算所有日期的 ztd, 消除每日重复 SQLite 查询.

    dates: 回测窗口内所有交易日 (YYYY-MM-DD)
    all_symbols: 全量股票代码列表
    """
    global _ztd_cache
    _ztd_cache.clear()
    if not dates or not all_symbols:
        return

    import pandas as pd
    earliest = pd.Timestamp(min(dates)) - pd.Timedelta(days=_require_cfg("data.lookback_days") + 10)
    latest = pd.Timestamp(max(dates))

    conn = DatabaseManager.market()
    ph = ",".join(["?"] * len(all_symbols))
    rows = conn.execute(
        f"""SELECT date, symbol, volume
            FROM daily
            WHERE date BETWEEN ? AND ?
              AND symbol IN ({ph})
            ORDER BY symbol, date""",
        [earliest.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d")] + list(all_symbols)
    ).fetchall()
    conn.close()

    if not rows:
        _log.warning("preload_ztd_cache: no rows for %d symbols x %d days",
                    len(all_symbols), len(dates))
        return

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'volume'])
    df['date'] = pd.to_datetime(df['date'])

    # 向量化 (审计 P1-7, 2026-07-26): 原每日期全表过滤 + groupby
    # (134 dates 实测 91s) → 每股 rolling(250) 一次 + merge_asof 查表。
    # 语义: 每股最后 250 行 (≤d) 中零成交占比, 与原实现一致;
    # 唯一差异: volume NaN 行不计入 total (原按行数, daily.volume 实际无 NaN)。
    df = df.sort_values(['symbol', 'date'])
    df['zero'] = (df['volume'] == 0).astype(float)
    rolled = df.groupby('symbol', sort=False).rolling(250, min_periods=1)
    df['ztd'] = (rolled['zero'].sum() / rolled['volume'].count()).values
    df = df.sort_values('date')
    grid = pd.MultiIndex.from_product(
        [sorted(pd.Timestamp(d) for d in dates), df['symbol'].unique()],
        names=['date', 'symbol']).to_frame(index=False)
    merged = pd.merge_asof(grid, df[['date', 'symbol', 'ztd']],
                           on='date', by='symbol')
    for d in dates:
        s = (merged.loc[merged['date'] == pd.Timestamp(d)]
             .set_index('symbol')['ztd'].dropna())
        if s.empty:
            continue
        _ztd_cache[d] = s

    _log.info("preload_ztd_cache: precomputed %d dates for %d symbols",
             len(_ztd_cache), len(all_symbols))


def clear_ztd_cache():
    """释放 ztd 预计算缓存 (M1 8GB 硬约束: 2000 日期 × 5000 symbols ≈ 80MB)."""
    global _ztd_cache
    _ztd_cache.clear()
    _log.debug("ztd_cache: cleared")


def compute_ztd(data, date, window=250):
    """停牌比率: 过去 window 交易日中零成交天数占比, 取负号.

    Liu (2006): A股停牌是流动性风险的直接度量.
    零成交日=停牌日 (或流动性枯竭). 高分=低停牌=好流动性.

    数据源: daily.volume (日线成交量).
    """
    import sqlite3, pandas as pd, numpy as np

    close = data["close"]
    _syms = close.columns.tolist()

    # ── 优先使用预计算缓存 ──
    # v477: 缓存 key 为 str (preload_ztd_cache dates), dispatch 传入 Timestamp —
    #       Timestamp in dict 永远 False → 缓存永不命中 → 因子 0 行
    key = to_str(date) if not isinstance(date, str) else date
    if key in _ztd_cache:
        import numpy as np
        ztd = _ztd_cache[key].reindex(_syms)
        ztd.name = "ztd"
        ztd = ztd.where(ztd.notna(), other=np.nan)
        result = _cs_zscore(-ztd)
        result.name = "ztd"
        return result


    # ── 缓存未命中: 优雅跳过, 不在评估中崩溃 ──
    return pd.Series(np.nan, index=close.columns, name="ztd")



# ═══════════════════════════════════════════════════════════
# 20. 北向资金净流入 — 沪深港通资金流因子
#    A股验证: 华泰 2023, 中金 2022. 北向资金对次日收益有预测力.
# ═══════════════════════════════════════════════════════════



# 需要三表(资产负债表+利润表+现金流量表)合并数据的因子名
# 模板 2a: 这些因子接收 financials=DataFrame 参数, 不内部访问 DataStore
# 函数定义在文件上方, 此处位于 compute_all_factors 之后, 确保函数已定义


# ═══════════════════════════════════════════════════════════
# 21. SUE (标准化未预期盈余) — Bernard & Thomas (1989) PEAD
#    A股验证: 中信 2022. 季报盈余超预期→公告后漂移.
#    SUE = (EPS_t - EPS_{t-4q}) / σ(EPS_8q), 取正号 (高SUE→高分).
# ═══════════════════════════════════════════════════════════

def compute_day_night(data, date, night_window=10, intraday_window=20):
    """OIR 昼夜合成因子: 0.6×日内反转 + 0.4×隔夜跳空绝对值.
    
    华安证券(2020): 《昼夜分离，隔夜跳空与日内反转选股因子》.
    IC=-8.1%, ICIR=4.04, 月度胜率 89.6%.
    逻辑: T+1制度下日内收益反转(涨→跌), 隔夜跳空绝对值越大→未来收益越低.
    仅需日频 OHLC, akshare 免费.

    Args:
        night_window: 隔夜跳空回看窗口 (默认10日, 衰减快)
        intraday_window: 日内反转回看窗口 (默认20日, 衰减慢)
    """
    import numpy as np
    close = data["close"]
    open_ = data["open"]

    # 日内反转: 累计对数收益率, 取负 (IC为负)
    ret_intra = np.log(close.astype(float) / open_.astype(float))
    intra_rev = ret_intra.rolling(intraday_window, min_periods=10).sum()

    # 隔夜跳空: 绝对值累计 (无论高开低开, 跳空幅度大→次月反转)
    ret_night = np.log(open_.astype(float) / close.shift(1).astype(float))
    night_jump = ret_night.abs().rolling(night_window, min_periods=5).sum()

    # B2 (2026-08-18): 原 .iloc[-1] 恒取 chunk 末行 → 前视. rolling 序列按 date 定位.
    if date not in close.index:
        return None
    idx = close.index.get_loc(date)
    raw = 0.6 * intra_rev.iloc[idx] + 0.4 * night_jump.iloc[idx]
    # 取负: 因子值越小 (越负) → 买入信号越强
    return _cs_zscore(-raw).rename("day_night")


def compute_str(data, date, window=20, aux=None):
    """STR 量稳换手率: 过去 window 日换手率的标准差, 取负, 市值中性化.
    
    东吴证券(2021): 《量稳换手率选股因子——量小、量缩，都不如量稳？》.
    IC=-7.9%, IR=2.96, 胜率 77.6%.
    逻辑: 换手率波动大→未来收益低, 稳定性比绝对水平更有预测力.
    仅需日频换手率.

    Args:
        window: 标准差回看窗口 (默认20日, 匹配月频调仓; 10-60日均稳健)
        aux: 预加载辅助数据 dict, 含 stocks(total_mv) 用于市值中性化
    """
    import numpy as np
    close = data["close"]
    turnover_df = data["turnover"]

    if turnover_df.empty:
        return pd.Series(np.nan, index=close.columns, name="str")

    min_records = max(window // 2, 10)
    # B2 (2026-08-18): 原 iloc[-(window+1):] 恒取 chunk 尾部 (含未来行) → 前视.
    # 改为截到 date 为止的窗口.
    if date not in turnover_df.index:
        return pd.Series(np.nan, index=close.columns, name="str")
    idx = turnover_df.index.get_loc(date)
    tail = turnover_df.iloc[max(0, idx - window):idx + 1]
    raw = tail.rolling(window, min_periods=min_records).std().iloc[-1].dropna()
    valid_mask = tail.notna().sum() >= min_records
    raw = raw[valid_mask]
    raw.name = 'str'
    if raw.empty or raw.count() < 30:
        return _cs_zscore(-raw).rename("str")

    # 市值中性化 — ADR-043 layer1: 优先从 aux["stocks"] 取, 无 aux 时回退 DB
    if aux is not None and "stocks" in aux and not aux["stocks"].empty:
        stocks_df = aux["stocks"]
        if "total_mv" in stocks_df.columns:
            mv_map = stocks_df["total_mv"].dropna().to_dict()
        else:
            mv_map = {}
    else:
        conn2 = DatabaseManager.market()
        _syms2 = raw.index.tolist()
        _ph2 = ",".join(["?"] * len(_syms2))
        rows = conn2.execute(
            f"SELECT symbol, total_mv FROM stocks WHERE symbol IN ({_ph2}) AND total_mv IS NOT NULL",
            _syms2
        ).fetchall()
        conn2.close()
        mv_map = {r[0]: r[1] for r in rows}
    log_mv = pd.Series({s: np.log(np.float64(mv_map[s])) for s in raw.index if s in mv_map})
    common = raw.index.intersection(log_mv.index)
    if len(common) >= 30:
        from sklearn.linear_model import LinearRegression
        X = log_mv.loc[common].values.reshape(-1, 1)
        y = raw.loc[common].values
        resid = y - LinearRegression().fit(X, y).predict(X)
        raw = pd.Series(resid, index=common)

    # 取负: 低波动→高分
    return _cs_zscore(-raw).rename("str")


def compute_abn_turnover(data, date, window=20, aux=None):
    """ABN_TURN 异常换手率残差: 对 ln(Turnover) 做市值+行业回归取残差, 取负.
    
    Chordia, Huh & Subrahmanyam (2007, JFE); 东方证券朱剑涛(2015)首次引入A股.
    IC=-6.77%, 与 STR 相关 0.3-0.5, 互补.
    逻辑: 剔除市值和行业效应后的"真正异常"换手率 → 异常高换手→反转下跌.
    仅需日频换手率+市值+行业分类.

    Args:
        window: 换手率均值窗口 (默认20日)
        aux: 预加载辅助数据 dict, 含 stocks(total_mv, industry)
    """
    import numpy as np
    close = data["close"]
    turnover_df = data["turnover"]

    if turnover_df.empty:
        return pd.Series(np.nan, index=close.columns, name="abn_turnover")

    # 取市值 + 行业 — ADR-043 layer1: 优先从 aux["stocks"] 取
    syms = close.columns.tolist()
    if aux is not None and "stocks" in aux and not aux["stocks"].empty:
        stocks_df = aux["stocks"]
        mv_map = {}
        ind_map = {}
        if "total_mv" in stocks_df.columns:
            for sym in syms:
                if sym in stocks_df.index:
                    mv = stocks_df.loc[sym, "total_mv"]
                    if pd.notna(mv):
                        mv_map[sym] = mv
        if "industry" in stocks_df.columns:
            for sym in syms:
                if sym in stocks_df.index:
                    ind = stocks_df.loc[sym, "industry"]
                    if pd.notna(ind) and ind != "":
                        ind_map[sym] = ind
    else:
        conn = DatabaseManager.market()
        _ph = ",".join(["?"] * len(syms))
        meta_rows = conn.execute(f"""
            SELECT symbol, total_mv, industry FROM stocks
            WHERE symbol IN ({_ph})
        """, syms).fetchall()
        conn.close()
        mv_map = {r[0]: r[1] for r in meta_rows if r[1]}
        ind_map = {r[0]: r[2] for r in meta_rows if r[2]}

    min_records = max(window // 2, 10)
    # B2 (2026-08-18): 原 iloc[-(window+1):] 恒取 chunk 尾部 (含未来行) → 前视.
    # 改为截到 date 为止的窗口.
    if date not in turnover_df.index:
        return pd.Series(np.nan, index=close.columns, name="abn_turnover")
    idx = turnover_df.index.get_loc(date)
    tail = turnover_df.iloc[max(0, idx - window):idx + 1]
    avg_turn = tail.rolling(window, min_periods=min_records).mean().iloc[-1]
    valid_mask = tail.notna().sum() >= min_records
    avg_turn = avg_turn[valid_mask & (avg_turn > 0)]

    turn_series = pd.Series(np.log(np.asarray(avg_turn, dtype=np.float64)), index=avg_turn.index, name='ln_turnover')
    if turn_series.empty or turn_series.count() < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    # OLS: ln(Turnover) ~ ln(MktCap) + industry dummies
    common = [s for s in turn_series.index if s in mv_map]
    if len(common) < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    from sklearn.linear_model import LinearRegression
    import numpy as np
    y = turn_series.loc[common].values
    log_mv = np.log(np.asarray([mv_map[s] for s in common], dtype=np.float64))
    # 行业哑变量 (只保留有 ≥3 只股票的行业)
    industries = [ind_map.get(s, '') for s in common]
    ind_counts = pd.Series(industries).value_counts()
    valid_inds = ind_counts[ind_counts >= 3].index.tolist()
    ind_dummies = pd.get_dummies(industries)
    valid_cols = [c for c in ind_dummies.columns if c in valid_inds and c != '']
    if valid_cols:
        X = np.column_stack([log_mv, ind_dummies[valid_cols].values])
    else:
        X = log_mv.reshape(-1, 1)

    resid = y - LinearRegression().fit(X, y).predict(X)
    raw = pd.Series(resid, index=common)

    # 取负: 异常高换手→低分
    result = _cs_zscore(-raw)
    return pd.Series(result, index=result.index).rename("abn_turnover") if hasattr(result, 'rename') else result


def _get_limit_pool(date_str: str, conn=None):
    """读取 limit_up_pool 当日数据, 返回 (df_up, df_down) 或 (空df, 空df).

    优先使用传入 conn, 否则回退共享连接, 最后才开新连接.
    """
    own = False
    if conn is None:
        if _shared_limit_conn is not None:
            conn = _shared_limit_conn
        else:
            conn = _db_connect()
            own = True
    df_up = pd.read_sql_query(
        "SELECT * FROM limit_up_pool WHERE date=?", conn, params=(date_str,)
    )
    df_down = pd.read_sql_query(
        "SELECT * FROM limit_down_pool WHERE date=?", conn, params=(date_str,)
    )
    if own:
        conn.close()
    return df_up, df_down


def compute_seal_turnover_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """封成比: lock_capital / amount — 涨停封单金额与成交额之比.

    来源: 国金证券(2016), 华安证券(2026).
    实证: >10→连板概率>60%; <1→警惕炸板. 正向因子(封成比大→买入).
    """
    date_str = to_str(date)
    df_up, _ = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty:
        return pd.Series(0.0, index=symbols_all, name="seal_turnover_ratio")

    df_up = df_up.set_index("symbol")
    result = pd.Series(0.0, index=symbols_all)
    for sym in df_up.index.intersection(symbols_all):
        row = df_up.loc[sym]
        lock_cap = float(row.get("lock_capital", 0) or 0)
        amount = float(row.get("amount", 0) or 0)
        if amount > 0 and lock_cap > 0:
            result[sym] = lock_cap / amount

    return _cs_zscore(result).rename("seal_turnover_ratio")


def compute_seal_time(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """封板时间: 归一化首次涨停时间, 早封板=高分.

    来源: 国金证券(2016) — 封板时间与次日涨幅严格单调递减.
    公式: 1 - (first_time_min - 570) / 330 (9:30=570min, 15:00=900min)
    """
    date_str = to_str(date)
    df_up, _ = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty:
        return pd.Series(0.0, index=symbols_all, name="seal_time")

    df_up = df_up.set_index("symbol")
    result = pd.Series(0.0, index=symbols_all)
    for sym in df_up.index.intersection(symbols_all):
        row = df_up.loc[sym]
        ft = row.get("first_time", None)
        if ft is None or str(ft) == "nan" or str(ft) == "":
            continue
        t = str(ft).strip()
        parts = t.split(":")
        if len(parts) < 2:
            continue
        minutes = int(parts[0]) * 60 + int(parts[1])
        if minutes >= 570:  # 不早于 9:30
            result[sym] = 1.0 - (minutes - 570) / 330.0

    return _cs_zscore(result).rename("seal_time")


def compute_limit_touch_no_seal(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """触板未封: high >= pre_close*1.10*0.995 AND ret < 9.5% → 负信号.

    来源: 东方证券 / 涨跌停溢出效应研究 — 触板未封 = 假突破, 次日往往回落.
    向量化实现: 一次性计算所有股票, 不再逐只 Python 循环.
    """
    date_str = to_str(date)
    if "high" not in data.columns or "close" not in data.columns:
        return pd.Series(0.0, index=list(data["close"].columns), name="limit_touch_no_seal")

    close_df = data["close"]
    if date_str not in close_df.index:
        return pd.Series(0.0, index=list(close_df.columns), name="limit_touch_no_seal")

    date_idx = close_df.index.get_loc(date_str)
    if date_idx == 0:
        return pd.Series(0.0, index=list(close_df.columns), name="limit_touch_no_seal")

    # 当日高低收 + 昨日收盘 (全部向量化)
    today_high = data["high"].loc[date_str]      # Series[symbol]
    today_close = data["close"].loc[date_str]
    prev_close = data["close"].iloc[date_idx - 1]  # Series[symbol]

    # 对齐: 只处理三列都存在的股票
    common = today_high.index.intersection(today_close.index).intersection(prev_close.index)
    high = today_high[common]
    close = today_close[common]
    pre = prev_close[common]

    # 过滤无效前收盘
    mask = pre > 0
    high, close, pre = high[mask], close[mask], pre[mask]

    # 向量化计算 — 分板块涨停价 (test-v338: 修复科创/北交误判)
    # 688xxx=科创板20%, 300xxx=创业板20%, 8xx/4xx=北交所30%, 其余=主板10%
    def _board_limit_pct(sym):
        if sym.startswith(('8', '4')): return 1.30   # 北交所
        if sym.startswith('688'): return 1.20         # 科创板
        if sym.startswith('300'): return 1.20         # 创业板
        return 1.10                                    # 主板
    limit_pcts = pd.Series({s: _board_limit_pct(s) for s in pre.index}, dtype=float)
    limit_price = pre * limit_pcts
    ret = (close - pre) / pre
    # 触板判定: 达到涨停价99.5%且涨幅<涨停板×0.95 (分板块调整)
    hit = (high >= limit_price * 0.995) & (ret < limit_pcts * 0.95)

    result = pd.Series(0.0, index=list(close_df.columns))
    result[hit.index[hit]] = -1.0

    return _cs_zscore(result).rename("limit_touch_no_seal")


def compute_net_limit_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """净涨停占比: (n_up - n_down) / n_total — 市场情绪代理.

    来源: 开源证券 / DL合成因子 — 行业内涨跌停股净占比反映情绪溢出.
    """
    date_str = to_str(date)
    df_up, df_down = _get_limit_pool(date_str)
    symbols_all = list(data["close"].columns)

    if df_up.empty and df_down.empty:
        return pd.Series(0.0, index=symbols_all, name="net_limit_ratio")

    up_symbols = set(df_up["symbol"].tolist()) if not df_up.empty else set()
    down_symbols = set(df_down["symbol"].tolist()) if not df_down.empty else set()

    total = max(len(up_symbols) + len(down_symbols), 1)
    net = (len(up_symbols) - len(down_symbols)) / total

    result = pd.Series(float(net), index=symbols_all)
    return _cs_zscore(result).rename("net_limit_ratio")


# ═══════════════════════════════════════════════════════════
# P72: 数据源适配因子 — EPA估值异常 / TRCF换手率收敛 / 理想振幅
# ═══════════════════════════════════════════════════════════

def compute_trcf(data: "pd.DataFrame", date: str, window: int = 120) -> "pd.Series":
    """TRCF换手率收敛: -log(1 + std(MA5/10/20/60/120 turnover)).

    来源: 数据源适配报告 — ICIR=4.19, turnover 类最强.
    """
    symbols_all = list(data["close"].columns)

    if "turnover" not in data.columns:
        return pd.Series(0.0, index=symbols_all, name="trcf")

    turnover = data["turnover"]
    if date not in turnover.index:
        return pd.Series(0.0, index=symbols_all, name="trcf")

    windows = [5, 10, 20, 60, 120]
    result = pd.Series(0.0, index=symbols_all)

    for sym in symbols_all:
        if sym not in turnover.columns:
            continue
        ts = turnover[sym].dropna()
        if len(ts) < 120:
            continue
        mas = [ts.tail(w).mean() for w in windows]
        std_ma = np.std(mas)
        result[sym] = -np.log(1 + std_ma)

    if (result.abs() < 1e-10).all():
        return None  # 所有值为0 → 无有效数据
    return _cs_zscore(result).rename("trcf")


def compute_ideal_amplitude(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """理想振幅: -(avg(high 25% amp) - avg(low 25% amp)).

    来源: 开源证券 — ICIR~3.0, 波动率类最强.

    向量化 (审计 P1-7, 2026-07-26): 原逐股 python 循环 (5208×134 物化热点,
    实测 ~8s/日期) → numpy partition 批处理。语义等价于原实现
    (get_daily ffill 后 ampl 无中间 NaN, "全历史 dropna ≥ window" 与
    "近 window 行全有效" 等价); 显式 .loc[:date] 防全历史输入前视。
    停牌缺窗股票保守跳过 (原实现向更老日期取有效值, 有界语义差, 已记录)。
    """
    symbols_all = list(data["close"].columns)

    if "high" not in data.columns or "low" not in data.columns:
        return pd.Series(0.0, index=symbols_all, name="ideal_amplitude")

    ampl = ((data["high"] - data["low"]) / data["low"]).loc[:to_str(date)]
    ampl = ampl.replace([np.inf, -np.inf], np.nan)
    recent = ampl.tail(window)
    arr = recent.to_numpy(dtype=float)            # (window, n_syms)
    if arr.shape[0] < 3:
        return pd.Series(np.nan, index=symbols_all, name="ideal_amplitude")
    k = max(int(window * 0.25), 1)
    if k > arr.shape[0]:
        k = arr.shape[0] - 1
    valid = ~np.isnan(arr)
    cnt = valid.sum(axis=0)

    # top-k / bottom-k 均值 (NaN 以 ±inf 排除出选择)
    a = np.where(valid, arr, -np.inf)
    top = np.partition(a, len(arr) - k, axis=0)[-k:]
    top_mask = top > -np.inf
    high_q = (np.where(top_mask, top, 0).sum(axis=0)
              / np.where(top_mask.sum(axis=0) == 0, np.nan, top_mask.sum(axis=0)))
    b = np.where(valid, arr, np.inf)
    bot = np.partition(b, k - 1, axis=0)[:k]
    bot_mask = bot < np.inf
    low_q = (np.where(bot_mask, bot, 0).sum(axis=0)
             / np.where(bot_mask.sum(axis=0) == 0, np.nan, bot_mask.sum(axis=0)))

    result = pd.Series(0.0, index=symbols_all)
    vals = -(high_q - low_q)
    ok = (cnt >= window) & np.isfinite(pd.to_numeric(vals, errors='coerce'))
    result.loc[result.index[ok]] = vals[ok]
    return _cs_zscore(result).rename("ideal_amplitude")


# ══════════════════════════════════════════════════════════════
# P69: Factor maps moved to end of file
# (entries reference functions defined above, forward-reference safe)
# ══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# Gap 7: Alternative Data Phase 1 — 5 new factors from existing data
# ═══════════════════════════════════════════════════════════


def compute_short_interest(data, date, window=20, aux=None):
    """融券余额占比: short_balance / margin_total — 高融券比 = 市场看空。

    数据源: margin_detail 表 (data/margin.py).
    IC预估: 0.02-0.03, 负向因子 (高融券→低分).
    ADR-043 layer1: 优先从 aux["margin"] 取, 无 aux 时回退 DB.
    """
    import sqlite3, os as _os3
    symbols = list(data["close"].columns)
    result = pd.Series(np.nan, index=symbols)

    if aux is not None and "margin" in aux:
        margin = aux["margin"]
        if not margin.empty and "margin_total" in margin.columns and "short_balance" in margin.columns:
            # 取 ≤date 的最新日期行
            if "date" in margin.columns:
                margin_dates = pd.to_datetime(margin["date"])
                ts = pd.Timestamp(to_str(date))
                latest_mask = margin_dates <= ts
                if latest_mask.any():
                    latest_date = margin_dates[latest_mask].max()
                    latest_rows = margin[margin_dates == latest_date]
                else:
                    latest_rows = pd.DataFrame(columns=margin.columns)
            else:
                latest_rows = margin
            for _, row in latest_rows.iterrows():
                sym = row.get("symbol")
                sb = row.get("short_balance")
                mt = row.get("margin_total")
                if sym in symbols and pd.notna(mt) and mt > 0:
                    result[sym] = float(sb) / float(mt) if pd.notna(sb) else 0.0
            return _cs_zscore(-result).rename("short_interest")

    conn = DatabaseManager.market()
    rows = conn.execute(
        "SELECT symbol, short_balance, margin_total FROM margin_detail "
        "WHERE date = (SELECT MAX(date) FROM margin_detail WHERE date <= ?) "
        "AND margin_total > 0",
        (to_str(date),)
    ).fetchall()
    conn.close()
    for sym, sb, mt in rows:
        if sym in symbols and mt > 0:
            result[sym] = float(sb) / float(mt) if sb else 0
    # High short interest → negative signal
    return _cs_zscore(-result).rename("short_interest")


def compute_fund_flow_3m(data, date, window=60, aux=None):
    """基金持仓季度变动: 最近3个月基金持仓变化率。

    数据源: fund_hold 表 (data/fund_hold.py).
    IC预估: 0.02-0.03, 正向因子 (基金加仓→高分).
    ADR-043 layer1: 优先从 aux["fund_hold"] 做 60d 均值, 无 aux 时回退 DB.
    """
    import sqlite3, os as _os4
    symbols = list(data["close"].columns)
    result = pd.Series(0.0, index=symbols)

    if aux is not None and "fund_hold" in aux:
        fh = aux["fund_hold"]
        if not fh.empty and "change_ratio" in fh.columns and "symbol" in fh.columns:
            fh_clean = fh[fh["change_ratio"].notna()]
            if not fh_clean.empty:
                avg = fh_clean.groupby("symbol")["change_ratio"].mean()
                for sym in symbols:
                    if sym in avg.index:
                        result[sym] = avg[sym]
            if result.isna().all() or (result == 0).all():
                return None
            return _cs_zscore(result).rename("fund_flow_3m")

    conn = DatabaseManager.market()
    rows = conn.execute(
        "SELECT symbol, change_ratio FROM fund_hold "
        "WHERE report_date >= date(?, '-{} days') AND change_ratio IS NOT NULL "
        "ORDER BY symbol, report_date DESC".format(window),
        (to_str(date),)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    import pandas as _pd4
    df = _pd4.DataFrame(rows, columns=["symbol", "change_ratio"])
    for sym in symbols:
        sym_data = df[df["symbol"] == sym]
        if len(sym_data) > 0:
            result[sym] = sym_data["change_ratio"].mean()
    if result.isna().all() or (result == 0).all():
        return None
    return _cs_zscore(result).rename("fund_flow_3m")


# ═══════════════════════════════════════════════════════════
# test-v402: P1 缺失因子实现 — 5 个 evaluating 因子补 compute 函数
# ═══════════════════════════════════════════════════════════

def compute_abn_turnover_resid(data, date, window=20, aux=None):
    """异常换手率残差: turnover − cross-sectional median(turnover).

    与 abn_turnover 的区别: abn_turnover 用滚动均值和标准差,
    残差版用截面中位数差分, 对极端值更稳健。
    来源: 华泰金工(2022)《换手率类因子全解析》。
    IC预估: −5%~−7% (低残差换手→高分)。
    """
    close = data["close"] if isinstance(data.columns, pd.MultiIndex) else data
    volume = data["volume"] if isinstance(data.columns, pd.MultiIndex) and "volume" in data.columns.get_level_values(0) else None
    if volume is None or close is None or close.empty:
        return None
    if date not in volume.index:
        return None
    idx = volume.index.get_loc(date)
    start = max(0, idx - window + 1)
    avg_vol = volume.iloc[start:idx + 1].mean()
    if hasattr(close, 'columns') and not isinstance(close.columns, pd.MultiIndex):
        pass
    # 计算换手率代理: 成交量/流通市值 (简化: 用成交量替代)
    # 截面残差: volume_i − median(volume)
    latest_vol = volume.iloc[idx]
    if latest_vol.sum() == 0:
        return None
    median_vol = latest_vol.median()
    resid = latest_vol - median_vol
    result = -resid  # 低残差→高分
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result).rename("abn_turnover_resid")


def compute_overnight_gap_ratio(data, date, window=5):
    """隔夜跳空比率: avg((open − prev_close)/prev_close, N日).

    衡量隔夜跳空方向的持续性。
    来源: 华安证券(2020)《昼夜分离因子》。
    窗口默认 5 日 (周频调仓)。
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    opn = data["open"] if "open" in data.columns.get_level_values(0) else None
    close = data["close"]
    if opn is None or close is None or close.empty:
        return None
    if date not in close.index:
        return None
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    gaps = []
    for i in range(start, idx + 1):
        if i == 0:
            continue
        prev_c = close.iloc[i - 1]
        cur_o = opn.iloc[i]
        g = (cur_o - prev_c) / prev_c.replace(0, np.nan)
        gaps.append(g)
    if not gaps:
        return None
    avg_gap = pd.concat(gaps, axis=1).mean(axis=1) if len(gaps) > 1 else gaps[0]
    result = avg_gap  # 正向: 持续高开→高分
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result).rename("overnight_gap_ratio")


def compute_price_channel_position(data, date, window=20):
    """价格通道位置: (close − lowest_N) / (highest_N − lowest_N).

    Donchian Channel 位置指标。
    接近通道顶→可能超买, 接近通道底→可能超卖。
    取负: 低位→高分 (均值回复逻辑)。
    来源: Donchian (1960); WorldQuant 101 Alphas. IC预估: −4%~−6%。
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    close = data["close"]
    high = data["high"] if "high" in data.columns.get_level_values(0) else None
    low = data["low"] if "low" in data.columns.get_level_values(0) else None
    if close is None or high is None or low is None:
        return None
    if date not in close.index:
        return None
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    hh = high.iloc[start:idx + 1].max()
    ll = low.iloc[start:idx + 1].min()
    cc = close.iloc[idx]
    rng = hh - ll
    position = (cc - ll) / rng.replace(0, np.nan)
    result = -position  # 低位→高分 (均值回复)
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result).rename("price_channel_position")


def compute_qlib_vema(data, date, window=20):
    """量权EMA: EMA(close × volume) / EMA(volume) − close.

    Qlib 风格量价背离因子。量权均价偏离收盘价 → 量价分歧。
    正偏离 → 收盘价低于量权均价 → 可能低估。
    来源: Microsoft Qlib (2020); WorldQuant Alpha#2 变体。
    IC预估: +3%~+5% (正偏离→高分)。
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    close = data["close"]
    volume = data["volume"] if "volume" in data.columns.get_level_values(0) else None
    if close is None or volume is None or close.empty:
        return None
    if date not in close.index:
        return None
    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)
    close_win = close.iloc[start:idx + 1]
    vol_win = volume.iloc[start:idx + 1]
    # EMA with span=window
    span = window
    cv = close_win * vol_win
    ema_cv = cv.ewm(span=span, adjust=False).mean().iloc[-1]
    ema_v = vol_win.ewm(span=span, adjust=False).mean().iloc[-1]
    vema = ema_cv / ema_v.replace(0, np.nan)
    result = vema - close_win.iloc[-1]  # 量权均价 − 收盘价
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result).rename("qlib_vema")


def compute_wq_alpha_006(data, date, window=10):
    """WorldQuant Alpha#006: −correlation(open, volume, 10).

    开盘价与成交量的负相关。
    高相关性→价量同步→趋势确认, 负相关性→价量背离→反转信号。
    Alpha#006 原文: −1 * correlation(open, volume, 10)。
    来源: WorldQuant (2015) 101 Formulaic Alphas.
    IC预估: −3%~−5% (负相关→高分, 反转逻辑)。
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    opn = data["open"] if "open" in data.columns.get_level_values(0) else None
    volume = data["volume"] if "volume" in data.columns.get_level_values(0) else None
    if opn is None or volume is None or opn.empty:
        return None
    if date not in opn.index:
        return None
    idx = opn.index.get_loc(date)
    start = max(0, idx - window + 1)
    opn_win = opn.iloc[start:idx + 1]
    vol_win = volume.iloc[start:idx + 1]
    # Rolling correlation per symbol
    corr = {}
    for sym in opn_win.columns:
        o = opn_win[sym].dropna()
        v = vol_win[sym].dropna()
        common = o.index.intersection(v.index)
        if len(common) >= max(window // 2, 3):
            corr[sym] = -o.loc[common].corr(v.loc[common])
    if not corr:
        return None
    result = pd.Series(corr, dtype=float)
    result = result.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(result).rename("wq_alpha_006")


# ═══════════════════════════════════════════════════════════
# Factor registration map
# ═══════════════════════════════════════════════════════════
