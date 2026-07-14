"""价量因子子模块。"""

import traceback

import numpy as np
import pandas as pd
import sqlite3
import os as _os
from typing import Optional

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
    earliest = pd.Timestamp(min(dates)) - pd.Timedelta(days=375)
    latest = pd.Timestamp(max(dates))

    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    ph = ",".join(["?"] * len(all_symbols))
    rows = conn.execute(
        f"""SELECT date, symbol, volume
            FROM daily
            WHERE date BETWEEN ? AND ?
              AND symbol IN ({ph})
            ORDER BY symbol, date""",
        [earliest.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d")] + list(all_symbols)
    ).fetchall()

    if not rows:
        _log.warning("preload_ztd_cache: no rows for %d symbols x %d days",
                    len(all_symbols), len(dates))
        return

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'volume'])
    df['date'] = pd.to_datetime(df['date'])

    for d in dates:
        cutoff = pd.Timestamp(d)
        sub = df[df['date'] <= cutoff]
        if sub.empty:
            continue
        sub = sub.sort_values(['symbol', 'date'], ascending=[True, False])
        recent = sub.groupby('symbol', sort=False).head(250)
        zero = recent.groupby('symbol')['volume'].apply(lambda x: (x == 0).sum())
        total = recent.groupby('symbol').size()
        _ztd_cache[d] = (zero / total)

    _log.info("preload_ztd_cache: precomputed %d dates for %d symbols",
             len(_ztd_cache), len(all_symbols))


def compute_ztd(data, date, window=250):
    """停牌比率: 过去 window 交易日中零成交天数占比, 取负号.

    Liu (2006): A股停牌是流动性风险的直接度量.
    零成交日=停牌日 (或流动性枯竭). 高分=低停牌=好流动性.

    数据源: daily.volume (日线成交量).
    """
    import sqlite3, pandas as pd

    close = data["close"]
    _syms = close.columns.tolist()

    # ── 优先使用预计算缓存 ──
    if date in _ztd_cache:
        import numpy as np
        ztd = _ztd_cache[date].reindex(_syms)
        ztd.name = "ztd"
        ztd = ztd.where(ztd.notna(), other=np.nan)
        result = _cs_zscore(-ztd)
        result.name = "ztd"
        return result


    # ── 缓存未命中: fail-fast, 调用方忘记 preload_ztd_cache ──
    raise RuntimeError(f"ztd cache miss for {date}: preload_ztd_cache() must be called before compute_ztd")



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
    ret_intra = np.log(close / open_)
    intra_rev = ret_intra.rolling(intraday_window, min_periods=10).sum()

    # 隔夜跳空: 绝对值累计 (无论高开低开, 跳空幅度大→次月反转)
    ret_night = np.log(open_ / close.shift(1))
    night_jump = ret_night.abs().rolling(night_window, min_periods=5).sum()

    raw = 0.6 * intra_rev.iloc[-1] + 0.4 * night_jump.iloc[-1]
    # 取负: 因子值越小 (越负) → 买入信号越强
    return _cs_zscore(-raw).rename("day_night")


def compute_str(data, date, window=20):
    """STR 量稳换手率: 过去 window 日换手率的标准差, 取负, 市值中性化.
    
    东吴证券(2021): 《量稳换手率选股因子——量小、量缩，都不如量稳？》.
    IC=-7.9%, IR=2.96, 胜率 77.6%.
    逻辑: 换手率波动大→未来收益低, 稳定性比绝对水平更有预测力.
    仅需日频换手率.

    Args:
        window: 标准差回看窗口 (默认20日, 匹配月频调仓; 10-60日均稳健)
    """
    import numpy as np
    close = data["close"]
    turnover_df = data["turnover"]

    if turnover_df.empty:
        return pd.Series(np.nan, index=close.columns, name="str")

    min_records = max(window // 2, 10)
    raw = turnover_df.rolling(window, min_periods=min_records).std().iloc[-1].dropna()
    valid_mask = turnover_df.notna().sum() >= min_records
    raw = raw[valid_mask]
    raw.name = 'str'
    if raw.empty or raw.count() < 30:
        return _cs_zscore(-raw).rename("str")

    # 市值中性化 (从 stocks 表取 total_mv)
    conn2 = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    _syms2 = raw.index.tolist()
    _ph2 = ",".join(["?"] * len(_syms2))
    rows = conn2.execute(
        f"SELECT symbol, total_mv FROM stocks WHERE symbol IN ({_ph2}) AND total_mv IS NOT NULL",
        _syms2
    ).fetchall()
    mv_map = {r[0]: r[1] for r in rows}
    log_mv = pd.Series({s: np.log(mv_map[s]) for s in raw.index if s in mv_map})
    common = raw.index.intersection(log_mv.index)
    if len(common) >= 30:
        from sklearn.linear_model import LinearRegression
        X = log_mv.loc[common].values.reshape(-1, 1)
        y = raw.loc[common].values
        resid = y - LinearRegression().fit(X, y).predict(X)
        raw = pd.Series(resid, index=common)

    # 取负: 低波动→高分
    return _cs_zscore(-raw).rename("str")


def compute_abn_turnover(data, date, window=20):
    """ABN_TURN 异常换手率残差: 对 ln(Turnover) 做市值+行业回归取残差, 取负.
    
    Chordia, Huh & Subrahmanyam (2007, JFE); 东方证券朱剑涛(2015)首次引入A股.
    IC=-6.77%, 与 STR 相关 0.3-0.5, 互补.
    逻辑: 剔除市值和行业效应后的"真正异常"换手率 → 异常高换手→反转下跌.
    仅需日频换手率+市值+行业分类.

    Args:
        window: 换手率均值窗口 (默认20日)
    """
    import numpy as np
    close = data["close"]
    turnover_df = data["turnover"]

    if turnover_df.empty:
        return pd.Series(np.nan, index=close.columns, name="abn_turnover")

    # 取市值 + 行业
    syms = close.columns.tolist()
    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    _ph = ",".join(["?"] * len(syms))
    meta_rows = conn.execute(f"""
        SELECT symbol, total_mv, industry FROM stocks
        WHERE symbol IN ({_ph})
    """, syms).fetchall()

    mv_map = {r[0]: r[1] for r in meta_rows if r[1]}
    ind_map = {r[0]: r[2] for r in meta_rows if r[2]}

    min_records = max(window // 2, 10)
    avg_turn = turnover_df.rolling(window, min_periods=min_records).mean().iloc[-1]
    valid_mask = turnover_df.notna().sum() >= min_records
    avg_turn = avg_turn[valid_mask & (avg_turn > 0)]

    turn_series = np.log(avg_turn)
    turn_series.name = 'ln_turnover'
    if turn_series.empty or turn_series.count() < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    # OLS: ln(Turnover) ~ ln(MktCap) + industry dummies
    common = [s for s in turn_series.index if s in mv_map]
    if len(common) < 30:
        return _cs_zscore(-turn_series).rename("abn_turnover")

    from sklearn.linear_model import LinearRegression
    import numpy as np
    y = turn_series.loc[common].values
    log_mv = np.log([mv_map[s] for s in common])
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
    return _cs_zscore(-raw).rename("abn_turnover")


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
    return df_up, df_down


def compute_seal_turnover_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """封成比: lock_capital / amount — 涨停封单金额与成交额之比.

    来源: 国金证券(2016), 华安证券(2026).
    实证: >10→连板概率>60%; <1→警惕炸板. 正向因子(封成比大→买入).
    """
    date_str = str(date)[:10]
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
    date_str = str(date)[:10]
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
    date_str = str(date)[:10]
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

    # 向量化计算
    limit_price = pre * 1.10
    ret = (close - pre) / pre
    hit = (high >= limit_price * 0.995) & (ret < 0.095)

    result = pd.Series(0.0, index=list(close_df.columns))
    result[hit.index[hit]] = -1.0

    return _cs_zscore(result).rename("limit_touch_no_seal")


def compute_net_limit_ratio(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """净涨停占比: (n_up - n_down) / n_total — 市场情绪代理.

    来源: 开源证券 / DL合成因子 — 行业内涨跌停股净占比反映情绪溢出.
    """
    date_str = str(date)[:10]
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

    return _cs_zscore(result).rename("trcf")


def compute_ideal_amplitude(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """理想振幅: -(avg(high 25% amp) - avg(low 25% amp)).

    来源: 开源证券 — ICIR~3.0, 波动率类最强.
    """
    symbols_all = list(data["close"].columns)

    if "high" not in data.columns or "low" not in data.columns:
        return pd.Series(0.0, index=symbols_all, name="ideal_amplitude")

    result = pd.Series(0.0, index=symbols_all)

    for sym in symbols_all:
        if sym not in data["high"].columns or sym not in data["low"].columns:
            continue
        high = data["high"][sym].dropna()
        low = data["low"][sym].dropna()
        ampl = (high - low) / low
        ampl = ampl.dropna()
        if len(ampl) < window:
            continue
        recent = ampl.tail(window)
        high_q = recent.nlargest(max(int(len(recent) * 0.25), 1)).mean()
        low_q = recent.nsmallest(max(int(len(recent) * 0.25), 1)).mean()
        result[sym] = -(high_q - low_q)

    return _cs_zscore(result).rename("ideal_amplitude")


# ══════════════════════════════════════════════════════════════
# P69: Factor maps moved to end of file
# (entries reference functions defined above, forward-reference safe)
# ══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# Gap 7: Alternative Data Phase 1 — 5 new factors from existing data
# ═══════════════════════════════════════════════════════════


def compute_short_interest(data, date, window=20):
    """融券余额占比: short_balance / margin_total — 高融券比 = 市场看空。

    数据源: margin_detail 表 (data/margin.py).
    IC预估: 0.02-0.03, 负向因子 (高融券→低分).
    """
    import sqlite3, os as _os3
    symbols = list(data["close"].columns)
    result = pd.Series(np.nan, index=symbols)
    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    rows = conn.execute(
        "SELECT symbol, short_balance, margin_total FROM margin_detail "
        "WHERE date = (SELECT MAX(date) FROM margin_detail WHERE date <= ?) "
        "AND margin_total > 0",
        (str(date)[:10],)
    ).fetchall()
    for sym, sb, mt in rows:
        if sym in symbols and mt > 0:
            result[sym] = float(sb) / float(mt) if sb else 0
    # High short interest → negative signal
    return _cs_zscore(-result).rename("short_interest")


def compute_fund_flow_3m(data, date, window=60):
    """基金持仓季度变动: 最近3个月基金持仓变化率。

    数据源: fund_hold 表 (data/fund_hold.py).
    IC预估: 0.02-0.03, 正向因子 (基金加仓→高分).
    """
    import sqlite3, os as _os4
    symbols = list(data["close"].columns)
    result = pd.Series(0.0, index=symbols)
    conn = DatabaseManager.get_instance().get_connection("quant/data/market.db")
    rows = conn.execute(
        "SELECT symbol, change_ratio FROM fund_hold "
        "WHERE report_date >= date(?, '-{} days') AND change_ratio IS NOT NULL "
        "ORDER BY symbol, report_date DESC".format(window),
        (str(date)[:10],)
    ).fetchall()
    if rows:
        import pandas as _pd4
        df = _pd4.DataFrame(rows, columns=["symbol", "change_ratio"])
        for sym in symbols:
            sym_data = df[df["symbol"] == sym]
            if len(sym_data) > 0:
                result[sym] = sym_data["change_ratio"].mean()
    return _cs_zscore(result).rename("fund_flow_3m")



# ═══════════════════════════════════════════════════════════
# Factor registration map
# ═══════════════════════════════════════════════════════════
