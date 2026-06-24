"""信号辅助算法 — z-score峰值检测 / MA逆序 / LIS/LDS.
来源: 《35岁程序员的退路：量化投资学习笔记》第7-8章
"""
import math, sqlite3, os
import numpy as np

MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def zscore_peaks(closes: list, lag: int = 30, threshold: float = 3.5,
                 influence: float = 0.0) -> tuple:
    """鲁棒z-score峰值检测 (来源: StackOverflow 规范实现 + PLOS ONE 2025).
    参数标准值: lag=30, threshold=3.5(对应~1/2128误报率), influence=0.0(最稳健)

    返回: (signals, avgFilter, stdFilter)
      signals[i] = 1(峰值高点)/-1(峰值低点)/0(非极值)
    """
    n = len(closes)
    if n <= lag:
        return [0] * n, [0.0] * n, [0.0] * n

    signals = np.zeros(n, dtype=int)
    avg_filter = np.zeros(n)
    std_filter = np.zeros(n)
    filtered_y = np.array(closes, dtype=float)

    avg_filter[lag - 1] = np.mean(closes[:lag])
    std_filter[lag - 1] = np.std(closes[:lag])

    for i in range(lag, n):
        if abs(closes[i] - avg_filter[i - 1]) > threshold * std_filter[i - 1]:
            signals[i] = 1 if closes[i] > avg_filter[i - 1] else -1
            filtered_y[i] = influence * closes[i] + (1 - influence) * filtered_y[i - 1]
        else:
            signals[i] = 0
            filtered_y[i] = closes[i]
        avg_filter[i] = np.mean(filtered_y[i - lag:i])
        std_filter[i] = np.std(filtered_y[i - lag:i])

    return signals.tolist(), avg_filter.tolist(), std_filter.tolist()


def ma_inversion_score(symbol: str, periods: tuple = (5, 10, 20, 30),
                       mc: sqlite3.Connection = None) -> float:
    """MA多空逆序强度 (来源: 知乎专栏 量化实现).
    计算指定周期均线的逆序数, 归一化到0-100.
    默认周期 (5,10,20,30) — 来源: A股常规短期均线体系

    返回: 0-100, >50多头, <50空头, =50中性.
    """
    close_db = mc is None
    if close_db:
        mc = sqlite3.connect(MARKET_DB)

    max_period = max(periods)
    rows = mc.execute(
        "SELECT close FROM daily WHERE symbol=? AND close > 0 ORDER BY date DESC LIMIT ?",
        (symbol, max_period + 1)).fetchall()
    if close_db: mc.close()
    if len(rows) < max_period: return 50.0

    closes = np.array([r[0] for r in reversed(rows)])

    # 计算指定周期均线
    ma_values = []
    for period in periods:
        if len(closes) >= period:
            ma_values.append(np.mean(closes[-period:]))

    # 计算逆序数: 前面 > 后面的对数
    n = len(ma_values)
    if n < 2: return 50.0
    inv = 0
    for i in range(n):
        for j in range(i + 1, n):
            if ma_values[i] > ma_values[j]:
                inv += 1

    # 归一化: 最大逆序数 = n*(n-1)/2
    max_inv = n * (n - 1) / 2.0
    return round((1 - inv / max_inv) * 100, 2) if max_inv > 0 else 50.0


def longest_run(prices: list) -> dict:
    """最长递增/递减序列 (来源: 动态规划算法).
    判断当前趋势是否在持续延伸.
    阈值3: 道氏理论 — 趋势需要≥3个依次抬高(或降低)的确认点才成立.
    (来源: Charles Dow, 1902; Fidelity Technical Analysis Basics)

    返回: {lis_len, lds_len, direction, is_extending}
      direction: 'up'=递增中, 'down'=递减中, 'sideways'=无方向
      is_extending: 当前序列是否仍在延伸
    """
    n = len(prices)
    if n < 2:
        return {"lis_len": n, "lds_len": n, "direction": "sideways", "is_extending": False}

    lis = [1] * n  # 以i结尾的最长递增长度
    lds = [1] * n  # 以i结尾的最长递减长度

    for i in range(1, n):
        for j in range(i):
            if prices[i] > prices[j]:
                lis[i] = max(lis[i], lis[j] + 1)
            elif prices[i] < prices[j]:
                lds[i] = max(lds[i], lds[j] + 1)

    lis_max = max(lis)
    lds_max = max(lds)
    recent_lis = lis[-1]
    recent_lds = lds[-1]

    # 判断方向
    if recent_lis >= 3 and recent_lis >= recent_lds:
        direction = "up"
    elif recent_lds >= 3 and recent_lds >= recent_lis:
        direction = "down"
    else:
        direction = "sideways"

    # 当前趋势是否还在延伸 (最后几点仍在增长)
    is_extending = (direction == "up" and prices[-1] > prices[-2]) or \
                   (direction == "down" and prices[-1] < prices[-2])

    return {
        "lis_len": lis_max, "lds_len": lds_max,
        "direction": direction, "is_extending": is_extending,
        "current_run": max(recent_lis, recent_lds),
    }
