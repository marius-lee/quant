"""妖股信号因子。

妖股特征不分市值大小，捕捉的是:
  1. 成交量突变 — 近期量能显著放大（主力进场）
  2. 价格突破 — 收盘价突破近期高点（趋势启动）
  3. 异常波动 — 波动率突然升高（股性激活）
  4. 连续强势 — 连续多日收阳/涨幅（资金接力）

每只股票计算 0-1 的妖股得分，高分 = 越像妖股启动前兆。
"""

import pandas as pd
import numpy as np


class DemonSignals:
    """妖股信号检测器"""

    def __init__(self, vol_window: int = 10, price_window: int = 30,
                 surge_window: int = 5):
        self.vol_window = vol_window        # 成交量基准窗口 (缩短: 妖股启动快)
        self.price_window = price_window    # 价格突破基准窗口 (缩短: 近期高点)
        self.surge_window = surge_window    # 短窗口（近期异常检测）

    def compute(self, close: pd.DataFrame,
                volume: pd.DataFrame = None,
                high: pd.DataFrame = None,
                low: pd.DataFrame = None) -> pd.DataFrame:
        """
        返回 (dates, stocks) DataFrame，每只股票每天的妖股得分 0-1
        """
        ret = close.pct_change()

        # ---- 1. 成交量突变得分 ----
        if volume is not None:
            vol_avg = volume.rolling(self.vol_window).mean()
            vol_ratio = volume / vol_avg.replace(0, np.nan)  # 量比
            vol_surge = vol_ratio.rolling(self.surge_window).max()  # 近期最大量比
            vol_score = (vol_surge - 1).clip(0, 10) / 10  # 量比上限放宽到10倍
        else:
            vol_score = pd.DataFrame(0.0, index=close.index, columns=close.columns)

        # ---- 2. 价格突破得分 ----
        # 收盘价相对于近期高点的位置
        recent_high = high.rolling(self.price_window).max() if high is not None else close.rolling(self.price_window).max()
        price_position = close / recent_high.replace(0, np.nan)  # 0-1，1=创新高
        # 近期加速突破
        breakout = (price_position - price_position.shift(self.surge_window)).clip(0, 1)

        # ---- 3. 异常波动得分 ----
        ret_std_short = ret.rolling(self.surge_window).std()
        ret_std_long = ret.rolling(self.price_window).std()
        vol_ratio_signal = (ret_std_short / ret_std_long.replace(0, np.nan) - 1).clip(0, 3) / 3

        # ---- 4. 连续强势得分 ----
        # 连续阳线数 / 5
        up_days = (ret > 0).astype(float)
        streak = up_days.rolling(self.surge_window).sum() / self.surge_window
        # 近期累计涨幅
        momentum = close.pct_change(self.surge_window).clip(0, 0.5) / 0.5  # 上限放宽到50%

        # ---- 综合妖股得分 ----
        score = (
            vol_score * 0.30 +       # 量能突变权重最高
            breakout * 0.30 +        # 价格突破同样重要
            vol_ratio_signal * 0.20 + # 波动率异常
            streak * 0.10 +          # 连续阳线
            momentum * 0.10          # 短期动量
        )
        return score.clip(0, 1)
