"""技术类因子（量价数据）。

包含: 动量、反转、波动率、换手率、流动性、均线偏离等。
"""

import pandas as pd
from factor.base import BaseFactor


class TechnicalFactors(BaseFactor):
    """技术因子计算"""

    def __init__(self, na_fill: str = "median", windows=(5, 10, 20, 60)):
        super().__init__(na_fill)
        self.windows = windows

    def compute(self, data: dict) -> pd.DataFrame:
        """data 需包含: close(DataFrame), volume(DataFrame), turnover(DataFrame)"""
        close = data["close"]
        factors = {}

        for w in self.windows:
            # 动量因子: 过去 w 日收益率
            factors[f"momentum_{w}d"] = close.pct_change(w)

        for w in self.windows:
            # 反转因子: 短期反转 (1日反转 / w日均线)
            factors[f"reversal_{w}d"] = -close.pct_change(1).rolling(w).mean()

        for w in self.windows:
            # 波动率因子: 日收益率标准差
            factors[f"volatility_{w}d"] = (
                close.pct_change().rolling(w).std()
            )

        for w in self.windows:
            # 换手率变化
            if "turnover" in data:
                turnover = data["turnover"]
                factors[f"turnover_chg_{w}d"] = (
                    turnover / turnover.rolling(w).mean() - 1
                )

        # 均线偏离: (收盘价 - N日均线) / N日均线
        for w in self.windows:
            ma = close.rolling(w).mean()
            factors[f"ma_deviation_{w}d"] = (close - ma) / ma

        # 流动性因子: 成交量的变化
        if "volume" in data:
            volume = data["volume"]
            for w in self.windows:
                factors[f"volume_ratio_{w}d"] = (
                    volume / volume.rolling(w).mean()
                )

        # 最大回撤因子
        for w in self.windows:
            rolling_max = close.rolling(w).max()
            factors[f"drawdown_{w}d"] = (close - rolling_max) / rolling_max

        factors_df = pd.concat(factors, axis=1)
        factors_df = factors_df.sort_index()
        return self.process(factors_df)


if __name__ == "__main__":
    # 测试
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol="000001", period="daily",
                            start_date="20230101", end_date="20240101",
                            adjust="qfq")
    df = df.rename(columns={"日期": "date", "收盘": "close",
                             "成交量": "volume", "换手率": "turnover"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    tf = TechnicalFactors()
    # 构造多股票格式
    close_df = pd.DataFrame({"000001": df["close"]})
    result = tf.compute({"close": close_df})
    print(result.tail())
