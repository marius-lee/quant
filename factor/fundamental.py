"""基本面因子 — 从 stocks 表读取 PE/PB/ROE/市值。

因子计算: 估值(EP/BP/SP)、盈利(ROE)、规模(log_mv)、行业(哑变量)
"""

import numpy as np
import pandas as pd
from factor.base import BaseFactor


class FundamentalCrossSection(BaseFactor):
    """仅用已有数据的基本面代理（不需 tushare daily_basic）"""

    def __init__(self, na_fill: str = "median", windows=(20, 60)):
        super().__init__(na_fill)
        self.windows = windows

    def compute(self, data: dict) -> pd.DataFrame:
        """
        从量价数据推算类基本面因子:
          - 规模: log(close * volume) 代理市值
          - 价值: close / ma(close, 60) 代理 PB
          - 质量: 毛利率代理 (high-low)/close 的稳定性
        """
        close = data["close"]
        volume = data.get("volume", close * 1e7)
        high = data.get("high", close * 1.02)
        low = data.get("low", close * 0.98)

        all_factors = {}

        # 日成交金额代理 — 对数日成交金额 (非市值，无流通股本数据)
        dollar_volume = np.log(close * volume + 1)
        all_factors["dollar_volume"] = dollar_volume

        for w in self.windows:
            # 价值代理 — 价格相对均线位置
            ma = close.rolling(w).mean()
            all_factors[f"value_proxy_{w}d"] = (close / ma - 1)

            # 质量代理 — 收益稳定性 (低波动=高质量)
            ret = close.pct_change()
            all_factors[f"quality_proxy_{w}d"] = -ret.rolling(w).std()

            # 成长代理 — 近期价格动量
            all_factors[f"growth_proxy_{w}d"] = close.pct_change(w)

        factors_df = pd.concat(all_factors, axis=1)
        factors_df.columns = pd.MultiIndex.from_product([
            list(all_factors.keys()), close.columns
        ])
        return self.process(factors_df)
