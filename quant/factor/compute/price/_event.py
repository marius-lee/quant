"""价量因子子模块。"""

import numpy as np
import pandas as pd
import os as _os
from typing import Optional

from quant.config.constants import (
    _get_board_limit, _get_limit_detection_pct,
    _LHB_WINDOW,
)
from quant.factor.registry import _cs_zscore

from quant.utils.logger import get_logger as _get_logger
from quant.data.store import market_conn as _market_conn

_log = _get_logger("factor.compute")


def compute_limit_up_proximity(data: "pd.DataFrame", date: str, window: int = 5, aux=None) -> "pd.Series":
    """涨停距离因子: 近期涨幅占涨跌停板比例的平均值.

    算法: avg(daily_return / board_limit, window).
    板块识别: 从 aux["stocks"] (预加载的 stocks 表元数据) 获取 market + name,
              ST 股主板 5% / 主板 10% / 科创创业板 20% / 北交所 30%.
    高分 = 近期持续接近涨停 → 强势股 → 动量延续.

    来源: A股涨跌停制度独有异象. 接近涨停板的股票存在动量溢出效应.
    修改: 2026-07-17 — 涨跌停幅度从 config.yaml 读取, aux["stocks"] 替代 DB 查询.
    """
    close = data["close"]

    if aux is None or "stocks" not in aux:
        raise ValueError("compute_limit_up_proximity requires aux['stocks']")

    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name=f"limit_up_prox_{window}d")

    idx = close.index.get_loc(date)
    start = max(0, idx - window + 1)

    ret = close.pct_change()
    ret_slice = ret.iloc[start:idx + 1]

    avg_proximity = {}
    for sym in close.columns:
        r = ret_slice[sym].dropna()
        if len(r) < 2:
            continue
        limit = _get_board_limit(sym, aux)
        prox = (r / limit).mean()
        avg_proximity[sym] = prox

    result = pd.Series(avg_proximity)
    return _cs_zscore(result).rename(f"limit_up_prox_{window}d")



# ═══════════════════════════════════════════════════════════
# 21. 涨停板因子 (Limit-Up) — A股最强动量异象
# 首板次日连板概率 30-40%, IC≈0.06-0.10 (A股独有)
# ═══════════════════════════════════════════════════════════

def compute_limit_up_streak(data: "pd.DataFrame", date: str, window: int = 0, aux=None) -> "pd.Series":
    """涨停连板因子: 从 data OHLCV 自算涨停 + 连板数(不依赖 limit_up_pool)。

    算法:
      - 涨停检测阈值: 板块涨跌停幅度 - 容差 (config.yaml factor.limit_detection_margin),
        从 aux["stocks"] 获取板块/ST 状态, config.yaml 读取幅度值
      - 涨停 = 日收益 >= 检测阈值 且 close == high
      - 连板数 = 从今日往前连续涨停的天数
      - 倒U型评分: 1连板→1, 2→3, 3→6, 4→10, 5→8, 6+→递减

    来源: A股涨跌停制度独有异象. 涨停板有显著动量溢出.
    修改: 2026-07-03 — 从 limit_up_pool 改为 data OHLCV 自算.
    修改: 2026-07-17 — 涨跌停阈值从 config.yaml 读取, aux["stocks"] 提供板块/ST 识别.
    """
    close = data["close"]
    high = data["high"]

    if aux is None or "stocks" not in aux:
        raise ValueError("compute_limit_up_streak requires aux['stocks']")

    # 匹配日期索引 (兼容 Timestamp 和 string)
    date_str = str(date)[:10]
    matched = [d for d in close.index if str(d)[:10] == date_str]
    if not matched:
        return pd.Series(dtype=float, name="zt_streak")

    idx = close.index.get_loc(matched[0])
    symbols = list(close.columns)

    # 往前看 5 个交易日判断连板 (最多 5 连板, 超过递减)
    lookback = 5
    start = max(0, idx - lookback)
    cw = close.iloc[start:idx + 1]
    hw = high.iloc[start:idx + 1]

    ret = cw.pct_change()  # row 0 = NaN (无前一日 close)

    # 涨停检测阈值: 各板块涨跌停幅度 - 容差, 从 aux["stocks"] + config.yaml
    limit_map = {sym: _get_limit_detection_pct(sym, aux) for sym in symbols}

    # 今日是否涨停
    today_ret = ret.iloc[-1] * 100
    today_close = cw.iloc[-1]
    today_high = hw.iloc[-1]

    scores = {}
    for sym in symbols:
        lim = limit_map[sym]
        r = today_ret.get(sym)
        c = today_close.get(sym)
        h = today_high.get(sym)
        if pd.isna(r) or pd.isna(c) or pd.isna(h):
            continue
        if not (r >= lim and c == h and h > 0):
            continue  # 今日未涨停 → 无信号

        # 往前数连板
        streak = 1
        for j in range(len(cw) - 2, -1, -1):
            rj = ret.iloc[j].get(sym)
            cj = cw.iloc[j].get(sym)
            hj = hw.iloc[j].get(sym)
            if pd.isna(rj) or pd.isna(cj) or pd.isna(hj):
                break
            if (rj * 100 >= lim) and (cj == hj and hj > 0):
                streak += 1
            else:
                break

        # 倒U型评分: 连板越多越强, 但 >=5 连板风险加大
        if streak <= 4:
            scores[sym] = streak * (streak + 1) / 2  # 1→1, 2→3, 3→6, 4→10
        else:
            scores[sym] = max(0, 10 - (streak - 4) * 2)  # 5→8, 6→6, 7→4, ...

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("zt_streak")


def compute_dt_streak(data: "pd.DataFrame", date: str, window: int = 0, aux=None) -> "pd.Series":
    """跌停连板因子: zt_streak 的镜像 — 从 data OHLCV 自算跌停 + 连板数。

    算法:
      - 跌停检测阈值: 负的板块涨跌停幅度 + 容差 (config.yaml factor.limit_detection_margin),
        从 aux["stocks"] 获取板块/ST 状态, config.yaml 读取幅度值
      - 跌停 = 日收益 <= 检测阈值 且 close == low
      - 连板数 = 从今日往前连续跌停的天数
      - 负向评分(镜像倒U): 1连板→-1, 2→-3, 3→-6, 4→-10, 5→-8, 6+→递减
      - 跌停后大概率继续下跌(A股实证~70%), 连板越多负信号越强

    来源: A股涨跌停制度独有异象. 跌停板有显著的负向动量溢出.
    添加: 2026-07-03 — zt_streak 镜像, Phase 7 P1.
    修改: 2026-07-17 — 涨跌停阈值从 config.yaml 读取, aux["stocks"] 提供板块/ST 识别.
    """
    close = data["close"]
    low = data["low"]

    if aux is None or "stocks" not in aux:
        raise ValueError("compute_dt_streak requires aux['stocks']")

    # 匹配日期索引
    date_str = str(date)[:10]
    matched = [d for d in close.index if str(d)[:10] == date_str]
    if not matched:
        return pd.Series(dtype=float, name="dt_streak")

    idx = close.index.get_loc(matched[0])
    symbols = list(close.columns)

    # 往前看 5 个交易日判断连板
    lookback = 5
    start = max(0, idx - lookback)
    cw = close.iloc[start:idx + 1]
    lw = low.iloc[start:idx + 1]

    ret = cw.pct_change()

    # 跌停检测阈值: 负的(板块涨跌停幅度 - 容差), 从 aux["stocks"] + config.yaml
    limit_map = {sym: -_get_limit_detection_pct(sym, aux) for sym in symbols}

    today_ret = ret.iloc[-1] * 100
    today_close = cw.iloc[-1]
    today_low = lw.iloc[-1]

    scores = {}
    for sym in symbols:
        lim = limit_map[sym]
        r = today_ret.get(sym)
        c = today_close.get(sym)
        lo = today_low.get(sym)
        if pd.isna(r) or pd.isna(c) or pd.isna(lo):
            continue
        if not (r <= lim and c == lo and lo > 0):
            continue  # 今日未跌停 → 无信号

        # 往前数连板
        streak = 1
        for j in range(len(cw) - 2, -1, -1):
            rj = ret.iloc[j].get(sym)
            cj = cw.iloc[j].get(sym)
            lj = lw.iloc[j].get(sym)
            if pd.isna(rj) or pd.isna(cj) or pd.isna(lj):
                break
            if (rj * 100 <= lim) and (cj == lj and lj > 0):
                streak += 1
            else:
                break

        # 负向评分(镜像倒U): 连板越多负得越强
        if streak <= 4:
            scores[sym] = -streak * (streak + 1) / 2  # 1→-1, 2→-3, 3→-6, 4→-10
        else:
            scores[sym] = -(max(0, 10 - (streak - 4) * 2))  # 5→-8, 6→-6, 7→-4, ...

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("dt_streak")


def compute_lhb_net_buy(data: "pd.DataFrame", date: str, window: int = _LHB_WINDOW, aux=None) -> "pd.Series":
    """龙虎榜净买入强度因子: total_net_buy / avg(circ_mv), N日窗口.

    算法:
      - 从 lhb_detail 表读取过去 N 个交易日龙虎榜记录
      - 每只股票: SUM(net_buy) / AVG(circ_mv) = 净买入占比
      - 截面 z-score 标准化
      - 未上榜股票得 0 分 (中性)

    来源: A股龙虎榜制度独有信号. 机构/游资上榜净买入是资金流入代理变量,
          龙虎榜净买入与次日收益正相关 (A股实证 IC≈0.04-0.08).

    添加日期: 2026-07-03 — lhb_detail 表补齐后激活.
    """
    symbols = list(data["close"].columns)
    date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]

    all_dates = sorted(data.index)
    if date_str not in all_dates:
        return pd.Series(np.nan, index=symbols, name=f"lhb_net_buy_{window}d")

    pos = all_dates.index(date_str)
    start = max(0, pos - window + 1)
    if hasattr(all_dates[start], "strftime"):
        start_date = all_dates[start].strftime("%Y-%m-%d")
    else:
        start_date = str(all_dates[start])[:10]

    if aux is None or "lhb" not in aux:
        raise ValueError("compute_lhb_net_buy requires preloaded aux['lhb']")

    lhb = aux["lhb"]
    mask = (lhb["trade_date"] >= start_date) & (lhb["trade_date"] <= date_str)
    w = lhb[mask]
    if w.empty:
        return pd.Series(0.0, index=symbols, name=f"lhb_net_buy_{window}d")

    grouped = w.groupby("symbol").agg(
        total_net_buy=("net_buy", "sum"),
        avg_circ_mv=("circ_mv", lambda x: x.dropna().mean())
    )
    scores = {}
    for sym, row in grouped.iterrows():
        if row["avg_circ_mv"] and row["avg_circ_mv"] > 0:
            scores[sym] = row["total_net_buy"] / row["avg_circ_mv"]

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename(f"lhb_net_buy_{window}d")



def compute_lhb_post_quality(data: "pd.DataFrame", date: str, window: int = 90, aux=None) -> "pd.Series":
    """LHB 上榜后质量因子: 历史上榜后 post_5d 平均收益。

    算法:
      - 从 lhb_detail 读取过去 window 天上榜记录 (排除最近5天, post_5d未实现)
      - 每只股票: AVG(post_5d) — 历史上榜后平均收益
      - 截面 z-score 标准化
      - 从未上榜股票: 0 (中性)
      - 至少上榜2次才纳入计算

    来源: A股龙虎榜制度独有信号. 历史上榜后持续涨的股票是真正的强势股,
          上榜后持续跌的是散户接盘 (A股实证: 上榜后平均跌0.87%).

    添加: 2026-07-03 — lhb_detail 表补齐后, post_5d 字段已有 24,386 行.
    """
    symbols = list(data["close"].columns)

    all_dates = [str(d)[:10] for d in sorted(data.index)]
    date_str = str(date)[:10]
    if date_str not in all_dates:
        return pd.Series(0.0, index=symbols, name="lhb_post_quality")

    idx = all_dates.index(date_str)
    start_idx = max(0, idx - window)
    end_idx = max(0, idx - 5)  # 排除最近5天 (post_5d 尚未实现)
    if end_idx <= start_idx:
        return pd.Series(0.0, index=symbols, name="lhb_post_quality")

    start_date = all_dates[start_idx]
    end_date = all_dates[end_idx]

    if aux is None or "lhb" not in aux:
        raise ValueError("compute_lhb_post_quality requires preloaded aux['lhb']")

    lhb = aux["lhb"]
    mask = (lhb["trade_date"] >= start_date) & (lhb["trade_date"] <= end_date) & lhb["post_5d"].notna()
    w = lhb[mask]
    if w.empty:
        return pd.Series(0.0, index=symbols, name="lhb_post_quality")

    grouped = w.groupby("symbol").agg(avg_post5=("post_5d", "mean"), n=("post_5d", "count"))
    qualified = grouped[grouped["n"] >= 2]
    scores = qualified["avg_post5"].to_dict()

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("lhb_post_quality")




def compute_margin_balance_chg(data: "pd.DataFrame", date: str, window: int = 5, aux=None) -> "pd.Series":
    """融资余额变化率: (今日余额 - window日前余额) / window日前余额。

    数据源: margin_detail 表 (融资融券每日明细)
    逻辑: 融资余额增加 → 杠杆资金看多 → 正向预期收益
    实证: A股融资余额变化率与次日收益 IC≈0.03-0.06

    添加: 2026-07-03 — Phase 8 P2, margin_detail 表同步后激活.
    """
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    all_dates = sorted(data.index)
    idx = None
    for i, d in enumerate(all_dates):
        if str(d)[:10] == date_str:
            idx = i
            break
    if idx is None or idx < window:
        return pd.Series(0.0, index=symbols, name="margin_balance_chg")

    prev_date = str(all_dates[idx - window])[:10]

    if aux is None or "margin" not in aux:
        raise ValueError("compute_margin_balance_chg requires preloaded aux['margin']")

    m = aux["margin"]
    today_rows = m[m["date"] == date_str]
    prev_rows = m[m["date"] == prev_date]

    today_map = {r["symbol"]: r["margin_balance"] for _, r in today_rows.iterrows() if r["margin_balance"] and r["margin_balance"] > 0}
    prev_map = {r["symbol"]: r["margin_balance"] for _, r in prev_rows.iterrows() if r["margin_balance"] and r["margin_balance"] > 0}

    scores = {}
    for sym in symbols:
        t = today_map.get(sym)
        p = prev_map.get(sym)
        if t and p and p > 0:
            scores[sym] = (t - p) / p

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("margin_balance_chg")


def compute_margin_buy_ratio_price(data: "pd.DataFrame", date: str, window: int = 5, aux=None) -> "pd.Series":
    """融资买入占比: AVG(margin_buy / margin_balance) over window 天。

    数据源: margin_detail 表
    逻辑: 融资买入占余额比高 → 杠杆资金活跃 → 正向预期收益
    实证: 融资买入占比与短期动量正相关 IC≈0.02-0.04

    命名: margin_buy_ratio_5d — 与单日版 margin_buy_ratio 区分, 避免重复注册。
    添加: 2026-07-03 — Phase 8 P2.
    """
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    dates = []
    for d in sorted(data.index):
        if str(d)[:10] < date_str:
            dates.append(str(d)[:10])
    if len(dates) < window:
        return pd.Series(0.0, index=symbols, name="margin_buy_ratio_5d")
    lookback_dates = dates[-window:]

    if aux is None or "margin" not in aux:
        raise ValueError("compute_margin_buy_ratio_price requires preloaded aux['margin']")

    m = aux["margin"]
    w = m[m["date"].isin(lookback_dates) & m["margin_balance"].notna() & (m["margin_balance"] > 0) & m["margin_buy"].notna()]
    if w.empty:
        return pd.Series(0.0, index=symbols, name="margin_buy_ratio_5d")
    w["ratio"] = w["margin_buy"] / w["margin_balance"]
    grouped = w.groupby("symbol")["ratio"].mean()
    scores = grouped.dropna().to_dict()
    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("margin_buy_ratio_5d")
def compute_main_flow_ratio(data: "pd.DataFrame", date: str, window: int = 5, aux=None) -> "pd.Series":
    """主力资金流向: AVG(main_net_ratio) over window 天。

    数据源: fund_flow 表 (个股资金流向)
    逻辑: 主力净流入占比高 → 聪明钱进场 → 正向预期收益
    实证: 主力资金净流入与短期收益正相关 IC≈0.03-0.05

    添加: 2026-07-03 — Phase 8 P2.
    """
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    dates = []
    for d in sorted(data.index):
        if str(d)[:10] < date_str:
            dates.append(str(d)[:10])
    if len(dates) < window:
        return pd.Series(0.0, index=symbols, name="main_flow_ratio")
    lookback_dates = dates[-window:]

    if aux is None or "fund_flow" not in aux:
        raise ValueError("compute_main_flow_ratio requires preloaded aux['fund_flow']")

    ff = aux["fund_flow"]
    w = ff[ff["date"].isin(lookback_dates) & ff["main_net_ratio"].notna()]
    if w.empty:
        return pd.Series(0.0, index=symbols, name="main_flow_ratio")
    grouped = w.groupby("symbol")["main_net_ratio"].mean()
    scores = grouped.dropna().to_dict()
    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("main_flow_ratio")



def compute_fund_change(data: "pd.DataFrame", date: str, window: int = 0, aux=None) -> "pd.Series":
    """基金持仓变动: 最新季报的持股变动比例 (change_ratio)。

    数据源: fund_hold 表 (季度基金持仓)
    逻辑: 基金增持 → 机构看好 → 正向预期收益
    实证: 机构持仓变动 IC≈+0.03~0.05
    频率: 季度更新, 窗口=120天(覆盖最近季度+披露滞后)

    添加: 2026-07-03 — Phase 9.
    """
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    if aux is None or "fund_hold" not in aux:
        raise ValueError("compute_fund_change requires preloaded aux['fund_hold']")

    fh = aux["fund_hold"]
    if fh.empty or "change_ratio" not in fh.columns:
        return pd.Series(0.0, index=symbols, name="fund_change")

    scores = {}
    for sym, row in fh.iterrows():
        if row.get("change_ratio") is not None:
            scores[sym] = float(row["change_ratio"])

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.0)
    return _cs_zscore(result).rename("fund_change")


def compute_analyst_buy(data: "pd.DataFrame", date: str, window: int = 0, aux=None) -> "pd.Series":
    """分析师看好度: 买入+增持占全部评级的比例。

    数据源: analyst_forecast 表 (全量分析师预测)
    逻辑: 买入/增持占比高 → 分析师共识看多 → 正向预期收益
    实证: 分析师评级修正 IC≈+0.04~0.07

    添加: 2026-07-03 — Phase 9.
    """
    symbols = list(data["close"].columns)
    date_str = str(date)[:10]

    if aux is None or "analyst" not in aux:
        raise ValueError("compute_analyst_buy requires preloaded aux['analyst']")

    a = aux["analyst"]
    if a.empty:
        return pd.Series(0.5, index=symbols, name="analyst_buy")

    scores = {}
    for sym, row in a.iterrows():
        buy = row.get("buy_count") or 0
        over = row.get("overweight_count") or 0
        neutral = row.get("neutral_count") or 0
        under = row.get("underweight_count") or 0
        total = buy + over + neutral + under
        if total > 0:
            scores[sym] = (buy + over) / total

    result = pd.Series(scores, dtype=float)
    result = result.reindex(symbols).fillna(0.5)  # 无数据 -> 中性
    return _cs_zscore(result).rename("analyst_buy")
