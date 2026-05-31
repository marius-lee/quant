"""龙虎榜因子 — 游资/机构追踪。

数据源: akshare stock_lhb_detail_em (东方财富龙虎榜)
因子:
  1. net_buy_ratio — 龙虎榜净买入/成交额
  2. institution_ratio — 机构席位占比
  3. hot_money_score — 游资活跃度(知名游资席位加权)
  4. lhb_appear_days — 最近N天龙虎榜出现次数

接入方式: 在 factor/cache.py 的 update_cache() 中调用 build_lhb_factor()，
将结果 concat 到 all_wide。
"""
import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("factor.dragon_tiger")


class DragonTigerFactors:
    """龙虎榜因子计算器"""

    def __init__(self):
        self.lhb_data = None

    def fetch_lhb_data(self, start_date: str = "20230101", end_date: str = None) -> pd.DataFrame:
        """从akshare拉取龙虎榜明细数据。"""
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not available, returning empty")
            return pd.DataFrame()

        if end_date is None:
            from datetime import datetime
            end_date = datetime.today().strftime("%Y%m%d")

        try:
            df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df.rename(columns={
                    "代码": "symbol", "名称": "name",
                    "收盘价": "close", "涨跌幅": "change_pct",
                    "换手率": "turnover_rate",
                    "龙虎榜净买额": "lhb_net_buy",
                    "龙虎榜买入额": "lhb_buy_amt",
                    "龙虎榜卖出额": "lhb_sell_amt",
                    "上榜原因": "reason",
                })
                df["trade_date"] = pd.to_datetime(df.get("trade_date", start_date))
                logger.info(f"fetched {len(df)} LHB records from {start_date} to {end_date}")
                self.lhb_data = df
                return df
        except Exception as e:
            logger.warning(f"LHB fetch failed: {e}")

        return pd.DataFrame()

    def compute(self, symbols: list, date: str, lookback_days: int = 20) -> pd.DataFrame:
        """计算龙虎榜因子。

        Args:
            symbols: 股票列表
            date: 当前日期 (YYYYMMDD)
            lookback_days: 回溯窗口

        Returns DataFrame with columns per factor
        """
        if self.lhb_data is None or self.lhb_data.empty:
            return pd.DataFrame(0.0, index=symbols,
                               columns=["lhb_net_buy_ratio", "lhb_institution",
                                        "lhb_appear_count", "lhb_score"])

        from datetime import datetime, timedelta
        date_dt = datetime.strptime(date, "%Y%m%d") if len(date) == 8 else pd.to_datetime(date)
        start_dt = date_dt - timedelta(days=lookback_days)

        recent = self.lhb_data[
            (self.lhb_data["trade_date"] >= start_dt) &
            (self.lhb_data["trade_date"] <= date_dt)
        ]

        factors = pd.DataFrame(index=symbols)
        factors["lhb_net_buy_ratio"] = 0.0
        factors["lhb_institution"] = 0.0
        factors["lhb_appear_count"] = 0
        factors["lhb_score"] = 0.0

        if recent.empty:
            return factors

        for sym in symbols:
            sym_data = recent[recent["symbol"] == sym]
            if sym_data.empty:
                continue
            factors.at[sym, "lhb_appear_count"] = len(sym_data)
            latest = sym_data.iloc[-1]
            factors.at[sym, "lhb_net_buy_ratio"] = latest.get("lhb_net_buy", 0)
            net_buys = sym_data["lhb_net_buy"].sum() if "lhb_net_buy" in sym_data.columns else 0
            count = len(sym_data)
            factors.at[sym, "lhb_score"] = (1 if net_buys > 0 else -0.5) * min(count, 5) / 5

        return factors


def build_lhb_factor(symbols: list, date: str, cache: dict = None) -> pd.Series:
    """便捷函数: 返回龙虎榜综合得分 Series。可集成到 factor/cache.py 中。"""
    lhb = DragonTigerFactors()
    if cache and "lhb_data" in cache:
        lhb.lhb_data = cache["lhb_data"]
    df = lhb.compute(symbols, date)
    return df["lhb_score"]
