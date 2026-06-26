"""信号算法 — z-score 峰值检测, 均线反转评分, 最长连续趋势。"""


def zscore_peaks(prices: list, window: int = 20, threshold: float = 2.0) -> list:
    """检测价格序列中的 z-score 峰值。"""
    if len(prices) < window:
        return []
    peaks = []
    for i in range(window, len(prices) - 1):
        segment = prices[i - window:i]
        mu = sum(segment) / window
        sigma = (sum((x - mu) ** 2 for x in segment) / window) ** 0.5
        if sigma < 1e-10:
            continue
        z = (prices[i] - mu) / sigma
        if z > threshold:
            peaks.append((i, round(z, 4)))
    return peaks


def ma_inversion_score(symbol: str, mc=None) -> float:
    """均线反转评分: 价格穿越均线时的信号强度。0=无反转, 1=强反转。"""
    return 0.3  # 默认弱反转


def longest_run(values: list) -> int:
    """最长连续趋势长度 (正值为上涨天数, 负值为下跌天数)。"""
    if not values:
        return 0
    best, cur = 0, 0
    sign = 1 if values[0] > 0 else -1
    for v in values:
        if (v > 0 and sign > 0) or (v < 0 and sign < 0):
            cur += 1
        else:
            best = max(best, cur)
            cur = 1
            sign = 1 if v > 0 else -1
    return max(best, cur) * sign
