"""多周期信号整合 — 周线方向 + 日线时机确认.

三级信号确认 (投票制):
  周线信号 (周五收盘算) → 决定方向 (多/空/中性)
  日线信号 (每日算)     → 决定时机
  最终权重 = w_daily × 日线得分 + w_weekly × 周线得分

  周线空头 → 日线多头信号 × 0 (完全压制)
  周线中性 → 日线信号 × 0.5 (半仓)

来源: Faber (2007) "A Quantitative Approach to Tactical Asset Allocation".
Adapted for A-share single-stock level.

Usage:
    from alpha.multi_tf import MultiTimeframeConfirmer
    confirmer = MultiTimeframeConfirmer()
    adjusted = confirmer.confirm(daily_signals, date)
"""

import os, sqlite3
import numpy as np
import pandas as pd
from utils.logger import get_logger
from config.constants import _require_cfg

_log = get_logger("alpha.multi_tf")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


class MultiTimeframeConfirmer:
    """周线+日线信号确认器.

    每个交易日检查周线方向, 压制逆势信号.
    """

    def __init__(self, weekly_weight: float = None):
        self.weekly_weight = weekly_weight or _require_cfg("alpha.weekly_weight")

    def _get_weekly_return(self, symbol: str, date: str) -> float:
        """获取周线收益率 (本周五 vs 上周五收盘价)."""
        conn = sqlite3.connect(_MARKET_DB)
        try:
            # 取上周五和本周五收盘价
            date_ts = pd.Timestamp(date)
            end_of_week = date_ts - pd.Timedelta(days=date_ts.dayofweek - 4)
            prev_week = end_of_week - pd.Timedelta(days=7)

            rows = conn.execute(
                "SELECT date, close FROM daily "
                "WHERE symbol=? AND date IN (?, ?) ORDER BY date DESC",
                (symbol, end_of_week.strftime("%Y-%m-%d"), prev_week.strftime("%Y-%m-%d"))
            ).fetchall()

            if len(rows) >= 2 and rows[0][1] > 0 and rows[1][1] > 0:
                return (rows[0][1] - rows[1][1]) / rows[1][1]
        finally:
            conn.close()
        return 0.0

    def _weekly_direction(self, symbols: list[str], date: str) -> pd.Series:
        """批量计算周线方向: +1(多), 0(中性), -1(空)."""
        result = pd.Series(0, index=symbols)

        for sym in symbols:
            ret = self._get_weekly_return(sym, date)
            if ret > 0.01:  # >1% → 多头
                result[sym] = 1
            elif ret < -0.01:  # <-1% → 空头
                result[sym] = -1
            else:
                result[sym] = 0

        return result

    def confirm(
        self,
        daily_signals: pd.Series,
        date: str,
    ) -> pd.Series:
        """用周线方向确认/压制日线信号.

        Args:
            daily_signals: 日线因子得分 (index=symbol, value=score)
            date: 当前日期 YYYY-MM-DD

        Returns:
            确认后的得分 Series (方向被压制的信号 × 0 或 × 0.5)
        """
        if len(daily_signals) == 0:
            return daily_signals

        try:
            symbols = list(daily_signals.index)
            weekly_dir = self._weekly_direction(symbols, date)

            adjusted = daily_signals.copy()

            for sym in symbols:
                wdir = weekly_dir.get(sym, 0)
                dal_score = daily_signals.get(sym, 0)

                if wdir == -1 and dal_score > 0:
                    # 周线空 + 日线多 → 压制为 0
                    adjusted[sym] = 0
                elif wdir == 0:
                    # 周线中性 → 日线信号 × 0.5 (半仓)
                    adjusted[sym] = dal_score * 0.5
                elif wdir == 1 and dal_score < 0:
                    # 周线多 + 日线空 → 中性
                    adjusted[sym] = 0

            n_suppressed = (daily_signals != 0) & (adjusted == 0) & (daily_signals.abs() > 0)
            if n_suppressed.any():
                _log.info(
                    f"MultiTimeframe({date}): suppressed {n_suppressed.sum()}/{len(daily_signals)} signals"
                )

            return adjusted
        except Exception as e:
            _log.warning(f"MultiTimeframe.confirm({date}): {e}")
            return daily_signals
