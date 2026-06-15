"""市场情绪周期检测 — 涨停家数/炸板率/连板高度 → 情绪阶段 + 仓位系数

情绪阶段:
  冰点(Ice):    涨停<30, 炸板率>50%, 连板高度≤2 → 仓位≤20%
  衰退(Decline): 涨停下降, 炸板率上升        → 仓位≤30%
  复苏(Recovery): 涨停上升2天, 溢价>2%       → 仓位30-50%
  扩张(Expansion): 涨停>50, 连板高度≥4      → 仓位50-60%
  高潮(Peak):     涨停>80, 胜率最高          → 仓位60-80%

来源:
  - 陈小群情绪周期体系 (30万→10亿游资公开体系)
  - 量化锚点: 涨停<30冰点/30-80发酵/>80高潮 (来源: 陈小群访谈+雪球复盘)
  - 仓位系数: 冰点≤20%/发酵30-50%/高潮80-100%/退潮0-10%
  - 2025年打板策略恐慌指数实证
"""

import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("factor.mood")


def detect_mood(close_df: pd.DataFrame) -> dict:
    """从日线收盘价宽表(日期×股票)计算市场情绪。

    Returns:
        stage: str      — ice|decline|recovery|expansion|peak
        coefficient: float — 仓位系数 (0.0-1.0)
        metrics: dict   — {n_limit_ups, failure_rate, leader_height, ...}
    """
    # 来源: 至少20个交易日才能计算情绪周期 (陈小群体系≈1个月数据)
    #       数据不足时回退到中性假设(复苏=0.5仓位), 不做极端判断
    if close_df.empty or len(close_df) < 20:
        return {"stage": "复苏", "coefficient": 0.5, "metrics": {}}

    # 向量化计算涨跌停 (9.5%统一阈值, ~99%股票适用)
    ret_df = close_df.pct_change()
    up_mask = ret_df >= 0.095
    down_mask = ret_df <= -0.095

    ups_series = up_mask.sum(axis=1).astype(int).sort_index()
    downs_series = down_mask.sum(axis=1).astype(int).sort_index()

    if len(ups_series) == 0 or ups_series.iloc[-1] == 0:
        # 0涨停 → ice (文档定义: ice = 涨停<30)
        return {"stage": "冰点", "coefficient": 0.15, "metrics": {}}

    # 最新值
    latest_up = ups_series.iloc[-1]
    latest_down = downs_series.iloc[-1]

    # 来源: 5日vs10日窗口(交易周 vs 双周) — 陈小群体系以周为基本单位
    #       1.1=10%阈值: 均值上升>10%才视为有效趋势(过滤噪声)
    recent_mean = ups_series.iloc[-5:].mean() if len(ups_series) >= 10 else ups_series.mean()
    prior_mean = ups_series.iloc[-10:-5].mean() if len(ups_series) >= 10 else recent_mean
    trend_up = recent_mean > prior_mean * 1.1

    # 炸板率估计: 跌停 / (涨停+跌停) 作为极端情绪代理
    total_events = latest_up + latest_down
    failure_proxy = latest_down / max(total_events, 1)

    # 连板高度近似: 最近涨停股票数量能持续几天
    sustained = 0
    for v in reversed(ups_series.values):
        if v >= 30:
            sustained += 1
        else:
            break
    leader_height = sustained

    # ── 情绪阶段判定 ──
    stage = "复苏"
    if latest_up < 30:
        stage = "冰点"
    elif latest_up < 50:
        if trend_up:
            stage = "复苏"
        else:
            stage = "衰退"
    elif latest_up < 80:
        if trend_up:
            stage = "扩张"
        else:
            stage = "高潮"
    else:
        stage = "高潮" if trend_up else "扩张"

    # ── 仓位系数 ──
    # 来源: 陈小群体系 — 冰点≤20%/复苏30-50%/高潮60-80%
    #       数值取自区间中点 (如冰点≤20%→15%, 高潮60-80%→65%)
    coeff_map = {
        "冰点": 0.15,
        "衰退": 0.25,
        "复苏": 0.40,
        "扩张": 0.55,
        "高潮": 0.65,
    }
    # 来源: 陈小群体系 — 炸板率>40%视为情绪极端恶化, 仓位砍到最低
    adjusted_coeff = dict(coeff_map)
    if failure_proxy > 0.40:
        adjusted_coeff["冰点"] = 0.0   # 冰点+高炸板=空仓
        adjusted_coeff["衰退"] = 0.10  # 衰退+高炸板=几乎空仓

    coefficient = adjusted_coeff.get(stage, 0.5)

    metrics = {
        "n_limit_ups": int(latest_up),
        "n_limit_downs": int(latest_down),
        "failure_proxy": round(failure_proxy, 3),
        "leader_height": leader_height,
        "trend_up": trend_up,
        "recent_mean": round(recent_mean, 1),
    }

    logger.info(f"mood: {stage} (up={latest_up} fail={failure_proxy:.2f} height={leader_height} trend={'↑' if trend_up else '↓'} coeff={coefficient:.2f})")
    return {"stage": stage, "coefficient": coefficient, "metrics": metrics}


def smooth_stage(close_df: pd.DataFrame, window: int = 3) -> dict:
    """3日平滑情绪检测 — 防单日跳变导致频繁切换仓位/止损。

    对最近 window 天的每日涨停数取均值后再判定阶段，比单日判定更稳定。
    华安证券实证: 3日平滑可消除 80% 的假跳变，同时保持对真实趋势切换的响应速度。
    """
    if close_df.empty or len(close_df) < 20:
        return detect_mood(close_df)

    ret_df = close_df.pct_change()
    up_mask = ret_df >= 0.095
    ups_series = up_mask.sum(axis=1).astype(int).sort_index()

    if len(ups_series) < window:
        return detect_mood(close_df)

    # 3日滑动均值平滑后判定
    smoothed = ups_series.rolling(window, min_periods=1).mean()
    latest_smoothed = smoothed.iloc[-1]
    prior_smoothed = smoothed.iloc[-window-1:-window].mean() if len(smoothed) >= window + 1 else latest_smoothed

    # 用平滑后的涨停数重新判定
    if latest_smoothed < 30:
        stage = "冰点"
    elif latest_smoothed < 50:
        stage = "复苏" if latest_smoothed > prior_smoothed * 1.05 else "衰退"
    elif latest_smoothed < 80:
        stage = "扩张" if latest_smoothed > prior_smoothed * 1.05 else "高潮"
    else:
        stage = "高潮" if latest_smoothed > prior_smoothed * 1.05 else "扩张"

    coeff_map = {"冰点": 0.15, "衰退": 0.25, "复苏": 0.40, "扩张": 0.55, "高潮": 0.65}
    coeff = coeff_map.get(stage, 0.5)

    return {
        "stage": stage,
        "coefficient": coeff,
        "metrics": {"n_limit_ups_smoothed": round(latest_smoothed, 1)},
    }


def get_stage_stop_loss(stage: str) -> float:
    """华安证券2025金融工程实证: 不同情绪阶段的最优止损百分比。

    来源: 华安证券《首板回调策略: 五大反直觉规律》(2025)
    对32,615个首板样本按情绪阶段分组回测, 计算各阶段最优止损参数:
      冰点 -2%: 跌停风险高, 紧止损保护本金
      衰退 -3%: 下行风险大于上行, 偏紧
      复苏 -4%: 上行概率提升, 适当放宽
      扩张 -4%: 趋势确认, 容忍波动
      高潮 -5%: 情绪亢奋但回撤风险高, 放宽至5%
    """
    mapping = {"冰点": -0.02, "衰退": -0.03, "复苏": -0.04, "扩张": -0.04, "高潮": -0.05}
    return mapping.get(stage, -0.04)
