"""佣金工具函数 — 陈小群体系共用。"""
from config.loader import get as cfg


def compute_commission(trade_value: float, is_sell: bool = False) -> tuple:
    """统一佣金模型: 万三费率, 最低5元/笔, 卖出加千一印花税。

    所有模拟交易、实盘执行模块必须使用此函数，确保绩效指标可比。

    Returns:
        (commission, stamp_tax, total_cost) 元
    """
    commission_rate = cfg("backtest.commission", 0.0003)
    min_commission = 5.0
    fee = max(min_commission, trade_value * commission_rate)
    stamp = trade_value * 0.001 if is_sell else 0.0
    return fee, stamp, fee + stamp
