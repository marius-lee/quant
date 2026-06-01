"""涨停板模式识别 — A股连板模式分析。

识别的模式:
  1. 首板 — 今日首次涨停(非一字板)
  2. 二板/三板/N板 — 连续涨停
  3. 一字板 — 开盘即涨停(无买入机会)
  4. T字板 — 盘中开板后回封(有买入机会)
  5. 炸板 — 涨停后开板未回封(危险信号)

输出每只股票每天的涨停状态和次日溢价概率评分。
"""
import pandas as pd
import numpy as np
from utils.logger import get_logger

logger = get_logger("factor.limit_up")


class LimitUpPatterns:
    """涨停板模式识别器 — 按板块区分涨跌停幅度"""

    # 板块 → 涨跌停幅度 (小数)
    BOARD_LIMIT = {
        "main": 0.10,       # 主板 10%
        "gem": 0.20,        # 创业板 (300/301)
        "star": 0.20,       # 科创板 (688)
        "bj": 0.30,         # 北交所 (4/8/92)
    }

    @staticmethod
    def _get_limit_pct(sym: str) -> float:
        """根据股票代码返回涨停幅度"""
        sym = str(sym).zfill(6)
        if sym.startswith(("30", "301")):
            return 0.20  # 创业板
        if sym.startswith("688"):
            return 0.20  # 科创板
        if sym.startswith(("4", "8", "92")):
            return 0.30  # 北交所
        return 0.10  # 主板 10%

    def __init__(self, limit_threshold: float = 0.095):
        self.limit_threshold = limit_threshold  # 兼容旧接口，实际使用 _get_limit_pct

    def detect(self, close: pd.DataFrame, high: pd.DataFrame,
               low: pd.DataFrame, open_: pd.DataFrame = None,
               volume: pd.DataFrame = None) -> pd.DataFrame:
        """检测每只股票每天的涨停状态。按板块使用不同的涨跌停阈值。"""
        prev_close = close.shift(1)
        chg = (close - prev_close) / prev_close

        # 按股票代码生成阈值矩阵 (跨板块)
        limit_thresholds = pd.Series({col: self._get_limit_pct(col) - 0.005
                                       for col in close.columns})
        is_limit = chg > limit_thresholds

        if open_ is not None:
            is_one_word = (open_ == high) & (high == low) & (low == close) & is_limit
        else:
            is_one_word = pd.DataFrame(False, index=close.index, columns=close.columns)

        if open_ is not None:
            limit_price = prev_close * (1 + limit_thresholds)
            touched_limit = high >= limit_price
            is_broken = touched_limit & ~is_limit
            is_t_word = is_limit & ~is_one_word & (low < high)
        else:
            is_broken = pd.DataFrame(False, index=close.index, columns=close.columns)
            is_t_word = pd.DataFrame(False, index=close.index, columns=close.columns)

        # 向量化计算连续涨停天数: per-column groupby-cumsum
        # 每列独立: is_limit=1累加, is_limit=0重置
        consecutive = is_limit.apply(
            lambda col: col.astype(int).groupby((~col).cumsum()).cumsum()
        )

        board_type = pd.DataFrame("", index=close.index, columns=close.columns)
        board_type[is_one_word] = "一字板"
        board_type[is_t_word] = "T字板"
        board_type[is_broken] = "炸板"
        # 向量化标注连板序号（只在未标注的空位填写）
        empty = (board_type == "")
        board_type[empty & (consecutive == 1)] = "首板"
        board_type[empty & (consecutive == 2)] = "二板"
        for n in range(3, 9):
            board_type[empty & (consecutive == n)] = f"{n}板"

        if volume is not None:
            # 20日均量，短历史(<20天)时自动退回到可用窗口长度
            vol_mean = volume.rolling(20, min_periods=5).mean()
            vol_ratio = (volume / vol_mean.replace(0, np.nan)).fillna(1.0).clip(0, 10)
            premium_score = (
                (is_limit.astype(float) * 0.4) +
                ((~is_one_word).astype(float) * 0.3) +
                (vol_ratio / 5 * 0.3)
            ).clip(0, 1)
        else:
            premium_score = (is_limit.astype(float) * 0.5 + (~is_one_word).astype(float) * 0.5).clip(0, 1)

        result = pd.concat({
            "is_limit_up": is_limit,
            "is_one_word": is_one_word,
            "is_t_word": is_t_word,
            "is_broken": is_broken,
            "consecutive": consecutive,
            "board_type": board_type,
            "next_day_premium": premium_score,
        }, axis=1)

        return result

    def next_day_filter(self, patterns: pd.DataFrame) -> pd.Series:
        """过滤: 哪些涨停股明天值得追 — 排除一字板/炸板, 保留首板/二板/T字板"""
        # patterns 是 MultiIndex columns (metric, stock) + DatetimeIndex rows
        # iloc[-1] 取最新日期行，得到 (metric, stock) MultiIndex Series
        # unstack(0) 还原为 DataFrame: columns=metrics, index=stocks
        latest = patterns.iloc[-1].unstack(0)
        buyable = ~latest["is_one_word"].astype(bool)
        safe = ~latest["is_broken"].astype(bool)
        cons = latest["consecutive"].astype(int)
        good_range = (cons >= 1) & (cons <= 4)
        score = latest["next_day_premium"].astype(float)
        score[~(buyable & safe & good_range)] = 0.0
        return score.sort_values(ascending=False)
