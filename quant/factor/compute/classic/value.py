"""价值因子: Alpha EP/BP/SP/CFB。v629: 改写为基本面接口 (fundamentals, date)。"""

import pandas as pd
import numpy as np
from quant.factor.registry import _cs_zscore

from quant.utils.logger import get_logger

logger = get_logger("factor.compute.classic.value")



def compute_alpha_ep(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """Earnings-to-Price ratio (Fama-French 1992). v629: 新接口改写 (fundamentals, date)。

    高 EP (低 PE) = 价值股 = 高分。
    数据来源: daily_valuation.pe_ttm, 回退 stocks.pe。
    """
    pe_col = "pe_ttm" if "pe_ttm" in fundamentals.columns and fundamentals["pe_ttm"].notna().any() else "pe"
    ep = 1.0 / fundamentals[pe_col].replace(0, np.nan)
    ep = ep.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(ep, sparse=True).rename("alpha_ep")


def compute_alpha_bp(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """Book-to-Price ratio (Fama-French 1992). v629: 新接口改写 (fundamentals, date)。

    高 BP (低 PB) = 价值股 = 高分。
    数据来源: daily_valuation.pb。
    """
    bp = 1.0 / fundamentals["pb"].replace(0, np.nan)
    bp = bp.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(bp, sparse=True).rename("alpha_bp")


def compute_alpha_sp(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """Sales-to-Price ratio (Basu 1977). v629: 新接口改写 (fundamentals, date)。

    高 SP (低 PS) = 价值股 = 高分。
    数据来源: daily_valuation.ps_ttm。
    """
    sp = 1.0 / fundamentals["ps_ttm"].replace(0, np.nan)
    sp = sp.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(sp, sparse=True).rename("alpha_sp")


def compute_alpha_cfp(fundamentals: "pd.DataFrame", date: str) -> "pd.Series":
    """Cash Flow-to-Price ratio (Lakonishok-Shleifer-Vishny 1994). v629: 新接口改写。

    高 CFP (低 PCF) = 价值股 = 高分。
    数据来源: daily_valuation.pcf_ttm。
    """
    cfp = 1.0 / fundamentals["pcf_ttm"].replace(0, np.nan)
    cfp = cfp.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(cfp, sparse=True).rename("alpha_cfp")
