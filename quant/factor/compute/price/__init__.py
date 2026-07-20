"""价量因子子包 — 向后兼容。"""

from quant.config.constants import *  # noqa: F401, F403

from quant.factor.compute.price._momentum import (
    compute_uret,  # noqa: F401
    _log_returns,
    compute_amihud,
    compute_downside_volatility,
    compute_hsgt_flow,
    compute_idiosyncratic_vol,
    compute_intraday_range,
    compute_ma_alignment,
    compute_max_return,
    compute_momentum,
    compute_money_flow,
    compute_overnight_gap,
    compute_residual_momentum,
    compute_reversal,
    compute_rsi_reversal,
    compute_skewness,
    compute_turnover_anomaly,
    compute_turnover_change,
    compute_turnover_reversal,
    compute_volatility,
    compute_volume_price_corr,
    compute_volume_ratio,
)

from quant.factor.compute.price._event import (  # noqa: F401
    compute_analyst_buy,
    compute_dt_streak,
    compute_fund_change,
    compute_lhb_net_buy,
    compute_lhb_post_quality,
    compute_limit_up_proximity,
    compute_limit_up_streak,
    compute_main_flow_ratio,
    compute_margin_balance_chg,
    compute_margin_buy_ratio_price,
)

from quant.factor.compute.price._sentiment import (
    compute_news_sentiment_1d,
    compute_news_volume_5d,
    compute_news_abnormal_20d,
)
from quant.factor.compute.price._turnover import (
    compute_ctr,
    compute_hl_volume,
    compute_turnover_accel,
)

from quant.factor.compute.price._alternative import (  # noqa: F401
    _get_limit_pool,
    compute_abn_turnover,
    compute_day_night,
    compute_fund_flow_3m,
    compute_ideal_amplitude,
    compute_limit_touch_no_seal,
    compute_net_limit_ratio,
    compute_seal_time,
    compute_seal_turnover_ratio,
    compute_short_interest,
    compute_str,
    compute_trcf,
    compute_ztd,
)


_PRICE_FN_MAP = {
    "reversal_5d":           (compute_reversal,            5),
    "turnover_rev_5d":       (compute_turnover_reversal,   5),
    "max_ret_20d":           (compute_max_return,         20),
    "gap_5d":                (compute_overnight_gap,       5),
    "range_20d":             (compute_intraday_range,     20),
    "momentum_63d":          (compute_momentum,           63),
    "residual_momentum_126d": (compute_residual_momentum,  126),  # Ch.3.7 Kakushadze & Serur 2018
    "momentum_126d":         (compute_momentum,          126),
    "momentum_252d":         (compute_momentum,          252),
    "volatility_126d":       (compute_volatility,        _VOLATILITY_WINDOW),
    "skewness_60d":          (compute_skewness,          _SKEWNESS_WINDOW),
    "idio_vol_126d":         (compute_idiosyncratic_vol, _IDIO_VOL_WINDOW),
    "amihud_250d":           (compute_amihud,            _AMIHUD_WINDOW),
    "rsi_rev_14d":           (compute_rsi_reversal,       14),
    "money_flow_5d":         (compute_money_flow,          5),
    "ma_alignment_20d":      (compute_ma_alignment,       20),
    "vol_price_corr_10d":    (compute_volume_price_corr,  10),
    "turnover_anomaly":      (compute_turnover_anomaly,    5),
    "limit_up_prox_5d":      (compute_limit_up_proximity,  5),
    "zt_streak":             (compute_limit_up_streak,     0),
    "dt_streak":             (compute_dt_streak,          0),
    "lhb_net_buy_20d":       (compute_lhb_net_buy,        20),
    "lhb_post_quality":      (compute_lhb_post_quality,   90),
    "margin_balance_chg":     (compute_margin_balance_chg, 5),
    "margin_buy_ratio_5d":    (compute_margin_buy_ratio_price,   5),
    "fund_change":             (compute_fund_change,        0),
    "analyst_buy":             (compute_analyst_buy,        0),
    # P69: 集中化 — 从动态注册迁移到静态 map
    "ztd":                    (compute_ztd,               250),
    "day_night":              (compute_day_night,          20),
    "str":                    (compute_str,                20),
    "abn_turnover":           (compute_abn_turnover,       20),
    "seal_turnover_ratio":    (compute_seal_turnover_ratio, 1),
    "seal_time":              (compute_seal_time,           1),
    "limit_touch_no_seal":    (compute_limit_touch_no_seal, 1),
    "net_limit_ratio":        (compute_net_limit_ratio,     1),
    "trcf":                   (compute_trcf,              120),
    "ideal_amplitude":        (compute_ideal_amplitude,     20),
    "short_interest":         (compute_short_interest,      20),
    "fund_flow_3m":           (compute_fund_flow_3m,        60),
    "news_sentiment_1d":      (compute_news_sentiment_1d,   1),
    "news_volume_5d":         (compute_news_volume_5d,      5),
    "news_abnormal_20d":      (compute_news_abnormal_20d,  20),
    # 幻方 Tier S 新因子 (2026-07-20)
    "ctr_20d":                (compute_ctr,               20),  # 东吴2024: IC=-7.6%
    "hl_volume_20d":          (compute_hl_volume,         20),  # 国盛2023: IC=-6.6%
    "turnover_accel":         (compute_turnover_accel,     5),  # 华安2024: IC=-10.5% (short/long=5/10)
    "uret_20d":               (compute_uret,              20),  # 东吴2023: IC=-5.4%
}



__all__ = [
    "_PRICE_FN_MAP",
    "_get_limit_pool",
    "_log_returns",
    "compute_abn_turnover",
    "compute_amihud",
    "compute_analyst_buy",
    "compute_day_night",
    "compute_downside_volatility",
    "compute_dt_streak",
    "compute_fund_change",
    "compute_fund_flow_3m",
    "compute_hsgt_flow",
    "compute_ideal_amplitude",
    "compute_idiosyncratic_vol",
    "compute_intraday_range",
    "compute_lhb_net_buy",
    "compute_lhb_post_quality",
    "compute_limit_touch_no_seal",
    "compute_limit_up_proximity",
    "compute_limit_up_streak",
    "compute_ma_alignment",
    "compute_main_flow_ratio",
    "compute_margin_balance_chg",
    "compute_margin_buy_ratio_price",
    "compute_max_return",
    "compute_momentum",
    "compute_money_flow",
    "compute_net_limit_ratio",
    "compute_overnight_gap",
    "compute_residual_momentum",
    "compute_reversal",
    "compute_rsi_reversal",
    "compute_seal_time",
    "compute_seal_turnover_ratio",
    "compute_short_interest",
    "compute_skewness",
    "compute_str",
    "compute_trcf",
    "compute_turnover_anomaly",
    "compute_turnover_change",
    "compute_turnover_reversal",
    "compute_volatility",
    "compute_volume_price_corr",
    "compute_volume_ratio",
    "compute_ztd",
    "compute_ctr",
    "compute_hl_volume",
    "compute_turnover_accel",
    "compute_uret",

    "compute_news_sentiment_1d",
    "compute_news_volume_5d",
    "compute_news_abnormal_20d",
]
