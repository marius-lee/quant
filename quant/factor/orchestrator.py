"""因子计算编排: 并行调度 compute_all_factors。

此模块不参与 import 链 — 运行时按需导入 factor.compute。
"""


def _get_load_functions():
    """延迟导入, 避免循环依赖。"""
    from quant.factor.compute import load_active_price_factors, load_active_fundamental_factors
    return load_active_price_factors, load_active_fundamental_factors


def get_factor_names(status_filter='using') -> list:
    """返回因子名列表 (从 factor_registry 表读取)。

    status_filter: 'using'→active+monitoring (实盘), 'active' (仅active), None (全部, 评估用).
    """
    load_price, load_fund = _get_load_functions()
    price_factors = load_price(status_filter)
    fund_factors = load_fund(status_filter)
    return list(price_factors.keys()) + list(fund_factors.keys())
