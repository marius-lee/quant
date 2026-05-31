"""分阶段策略管理 + 资金翻倍追踪。

阶段定义:
  Stage 0: ¥5000-1万    — 激进all-in妖股, 2-3只, 周频
  Stage 1: ¥1万-5万    — 激进+趋势, 3-5只, 周频
  Stage 2: ¥5万-20万   — 均衡, 5-8只, 双周频
  Stage 3: ¥20万-50万  — 稳健成长, 8-12只, 月频
  Stage 4: ¥50万-100万 — 私募级别, 12-20只, 月频+对冲
"""
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("strategy.planner")

STAGES = [
    {"name": "原始积累", "min": 5_000, "max": 10_000, "top_n": 3, "freq": "W",
     "ml_weight": 0.15, "neutralize": False, "max_drawdown": 0.80,
     "strategy": "妖股all-in — 只追最强信号, 2-3只集中, 周频换手"},
    {"name": "快速成长", "min": 10_000, "max": 50_000, "top_n": 5, "freq": "W",
     "ml_weight": 0.20, "neutralize": False, "max_drawdown": 0.60,
     "strategy": "激进+趋势 — 妖股主导, 适当分散3-5只, 周频"},
    {"name": "加速积累", "min": 50_000, "max": 200_000, "top_n": 8, "freq": "2W",
     "ml_weight": 0.25, "neutralize": False, "max_drawdown": 0.40,
     "strategy": "均衡偏激进 — 5-8只, 双周频, 加入龙虎榜因子"},
    {"name": "稳健成长", "min": 200_000, "max": 500_000, "top_n": 12, "freq": "M",
     "ml_weight": 0.30, "neutralize": True, "max_drawdown": 0.25,
     "strategy": "机构模式 — 8-12只, 月度再平衡, 开启中性化"},
    {"name": "冲关", "min": 500_000, "max": 1_000_000, "top_n": 20, "freq": "M",
     "ml_weight": 0.35, "neutralize": True, "max_drawdown": 0.15,
     "strategy": "私募级别 — 分散持仓+对冲, 追求稳定超额"},
]


def get_stage(capital: float) -> dict:
    """根据当前资金确定策略阶段"""
    for s in STAGES:
        if capital < s["max"]:
            return s
    return STAGES[-1]


def estimate_days_to_target(capital: float, target: float, daily_return: float) -> int:
    """估算以当前日收益率到达目标需要的天数"""
    if daily_return <= 0:
        return 99999
    import math
    return max(1, math.ceil(math.log(target / capital) / math.log(1 + daily_return)))


def get_strategy_config(capital: float) -> dict:
    """获取当前资金对应的策略配置，可直接覆盖config值"""
    stage = get_stage(capital)
    return {
        "stage_name": stage["name"],
        "current_capital": capital,
        "next_target": stage["max"],
        "top_n": stage["top_n"],
        "rebalance_freq": stage["freq"],
        "ml_weight": stage["ml_weight"],
        "neutralize": stage["neutralize"],
        "max_drawdown": stage["max_drawdown"],
        "strategy_desc": stage["strategy"],
    }


def compute_milestones(capital: float, daily_return: float = 0.02) -> list:
    """计算资金翻倍里程碑

    Args:
        capital: 当前资金
        daily_return: 日均收益率 (激进目标: 2%)

    Returns: [{target, days, date, progress_pct}]
    """
    milestones = [10_000, 50_000, 100_000, 200_000, 500_000, 1_000_000]
    today = datetime.now()
    result = []
    for m in milestones:
        if m <= capital:
            result.append({"target": m, "days": 0, "date": "已达成", "progress_pct": 100})
            continue
        days = estimate_days_to_target(capital, m, daily_return)
        target_date = today + timedelta(days=days)
        result.append({
            "target": m,
            "days": days,
            "date": target_date.strftime("%Y-%m-%d"),
            "progress_pct": round(capital / m * 100, 1),
        })
    return result
