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
    """涨停板模式识别器"""

    def __init__(self, limit_threshold: float = 0.095):
        self.limit_threshold = limit_threshold

    def detect(self, close: pd.DataFrame, high: pd.DataFrame,
               low: pd.DataFrame, open_: pd.DataFrame = None,
               volume: pd.DataFrame = None) -> pd.DataFrame:
        """检测每只股票每天的涨停状态。"""
        prev_close = close.shift(1)
        chg = (close - prev_close) / prev_close
        is_limit = chg > self.limit_threshold

        if open_ is not None:
            is_one_word = (open_ == high) & (high == low) & (low == close) & is_limit
        else:
            is_one_word = pd.DataFrame(False, index=close.index, columns=close.columns)

        if open_ is not None:
            limit_price = prev_close * (1 + self.limit_threshold)
            touched_limit = high >= limit_price
            is_broken = touched_limit & ~is_limit
            is_t_word = is_limit & ~is_one_word & (low < high)
        else:
            is_broken = pd.DataFrame(False, index=close.index, columns=close.columns)
            is_t_word = pd.DataFrame(False, index=close.index, columns=close.columns)

        consecutive = pd.DataFrame(0, index=close.index, columns=close.columns)
        for sym in close.columns:
            cnt = 0
            for i in range(len(close)):
                if is_limit.iloc[i][sym]:
                    cnt += 1
                else:
                    cnt = 0
                consecutive.iloc[i, consecutive.columns.get_loc(sym)] = cnt

        board_type = pd.DataFrame("", index=close.index, columns=close.columns)
        board_type[is_one_word] = "一字板"
        board_type[is_t_word] = "T字板"
        board_type[is_broken] = "炸板"
        for s in close.columns:
            for i in range(len(close)):
                c = consecutive.iloc[i, consecutive.columns.get_loc(s)]
                if c == 1 and board_type.iloc[i, board_type.columns.get_loc(s)] == "":
                    board_type.iloc[i, board_type.columns.get_loc(s)] = "首板"
                elif c == 2 and board_type.iloc[i, board_type.columns.get_loc(s)] == "":
                    board_type.iloc[i, board_type.columns.get_loc(s)] = "二板"
                elif c >= 3 and board_type.iloc[i, board_type.columns.get_loc(s)] == "":
                    board_type.iloc[i, board_type.columns.get_loc(s)] = f"{c}板"

        if volume is not None:
            vol_ratio = volume / volume.rolling(20).mean()
            premium_score = (
                (is_limit.astype(float) * 0.4) +
                ((~is_one_word).astype(float) * 0.3) +
                (vol_ratio.clip(0, 5) / 5 * 0.3)
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
        latest = patterns.xs(patterns.index.get_level_values(0)[-1], level=0)
        buyable = ~latest["is_one_word"].astype(bool)
        safe = ~latest["is_broken"].astype(bool)
        cons = latest["consecutive"].astype(int)
        good_range = (cons >= 1) & (cons <= 4)
        score = latest["next_day_premium"]
        score[~(buyable & safe & good_range)] = 0
        return score.sort_values(ascending=False)
