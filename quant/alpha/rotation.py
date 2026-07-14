"""行业轮动策略叠加 — 宏观→行业映射 + 行业内因子调权.

层 1: 宏观指标 → 行业超配/低配
  来源: 美林时钟 (Merrill Lynch 2004), A股改编.
  CPI↑ + PMI>50 → 超配 消费+工业, 低配 公用事业
  CPI↓ + PMI<50 → 超配 防御(医药), 低配 周期

层 2: 行业内因子调权
  周期行业: 增持 value 因子, 减持 momentum
  成长行业: 增持 momentum/ep, 减持 value

Usage:
    from alpha.rotation import SectorRotator
    rotator = SectorRotator()
    weights = rotator.overlay(alpha_scores, date, current_positions)
"""

import os, sqlite3
import numpy as np
import pandas as pd
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("alpha.rotation")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")

# ── 行业分类 → 美林时钟象限 ──
_CYCLICAL_INDS = {"有色金属", "钢铁", "化工", "建筑材料", "建筑装饰", "机械设备", "电气设备"}
_CONSUMER_INDS = {"食品饮料", "家用电器", "汽车", "纺织服装", "轻工制造", "商业贸易", "休闲服务"}
_DEFENSIVE_INDS = {"医药生物", "公用事业"}
_GROWTH_INDS = {"电子", "计算机", "传媒", "通信", "国防军工"}


class SectorRotator:
    """美林时钟行业轮动器.

    从 macro_indicator 表读取 PMI/CPI, 判断当前时钟象限,
    输出行业超配/低配权重向量.
    """

    def __init__(self):
        pass

    def _get_pmi(self, date: str) -> float:
        conn = sqlite3.connect(_MARKET_DB)
        row = conn.execute(
            "SELECT value FROM macro_indicator WHERE indicator='pmi_manufacturing' AND date<=? ORDER BY date DESC LIMIT 1",
            (date,)
        ).fetchone()
        conn.close()
        return row[0] if row else 50.0

    def _get_cpi(self, date: str) -> float:
        conn = sqlite3.connect(_MARKET_DB)
        row = conn.execute(
            "SELECT value FROM macro_indicator WHERE indicator='cpi_yoy' AND date<=? ORDER BY date DESC LIMIT 1",
            (date,)
        ).fetchone()
        conn.close()
        return row[0] if row else 0.0

    def _get_industries(self, symbols: list[str]) -> pd.Series:
        """获取股票行业分类 (从 stocks 表)."""
        conn = sqlite3.connect(_MARKET_DB)
        placeholders = ", ".join("?" * len(symbols))
        rows = conn.execute(
            f"SELECT symbol, industry FROM stocks WHERE symbol IN ({placeholders})",
            symbols
        ).fetchall()
        conn.close()
        return pd.Series({r[0]: r[1] for r in rows if r[1]})

    def _clock_quadrant(self, date: str) -> str:
        """判断美林时钟象限: expansion / overheating / contraction / recovery."""
        pmi = self._get_pmi(date)
        cpi = self._get_cpi(date)

        if pmi > 50 and cpi < 2:
            return "expansion"
        elif pmi > 50 and cpi >= 2:
            return "overheating"
        elif pmi <= 50 and cpi >= 2:
            return "contraction"
        else:
            return "recovery"

    def _industry_weights(self, quadrant: str) -> dict[str, float]:
        """根据时钟象限输出行业权重调整系数."""
        weights = {}

        if quadrant == "expansion":
            # 经济上行 + 低通胀 → 超配周期+消费
            for ind in _CYCLICAL_INDS:
                weights[ind] = 1.2
            for ind in _CONSUMER_INDS:
                weights[ind] = 1.1
            for ind in _DEFENSIVE_INDS:
                weights[ind] = 0.8
        elif quadrant == "overheating":
            # 经济上行 + 高通胀 → 超配资源+消费
            for ind in _CYCLICAL_INDS:
                weights[ind] = 1.15
            for ind in _CONSUMER_INDS:
                weights[ind] = 1.1
            for ind in _GROWTH_INDS:
                weights[ind] = 0.85
        elif quadrant == "contraction":
            # 经济下行 + 高通胀 → 超配防御
            for ind in _DEFENSIVE_INDS:
                weights[ind] = 1.3
            for ind in _CYCLICAL_INDS:
                weights[ind] = 0.7
            for ind in _GROWTH_INDS:
                weights[ind] = 0.75
        else:
            # recovery: 经济下行 + 低通胀 → 超配成长
            for ind in _GROWTH_INDS:
                weights[ind] = 1.25
            for ind in _CYCLICAL_INDS:
                weights[ind] = 0.85
            for ind in _DEFENSIVE_INDS:
                weights[ind] = 0.9

        return weights

    def overlay(
        self,
        alpha_scores: pd.Series,
        date: str,
    ) -> pd.Series:
        """将行业轮动权重叠加到因子得分上.

        Args:
            alpha_scores: 股票得分 (index=symbol, value=score)
            date: 当前日期 YYYY-MM-DD

        Returns:
            调整后的得分 Series
        """
        if len(alpha_scores) == 0:
            return alpha_scores

        quadrant = self._clock_quadrant(date)
        ind_weights = self._industry_weights(quadrant)
        industries = self._get_industries(list(alpha_scores.index))

        if len(industries) == 0:
            _log.debug(f"SectorRotator: no industry data for {date}")
            return alpha_scores

        # 对每只股票: alpha × 行业权重
        result = alpha_scores.copy()
        for sym in alpha_scores.index:
            ind = industries.get(sym, None)
            if ind in ind_weights:
                result[sym] *= ind_weights[ind]

        _log.info(f"SectorRotator({date}): quadrant={quadrant}, adjusted {len(result)} stocks")
        return result
