"""因子窗口工具: 从 _PRICE_FN_MAP 提取最大数据窗口。

基本面因子不依赖日线窗口, 跳过。默认下限 60 交易日。
"""

from factor.compute.price import _PRICE_FN_MAP


def max_factor_calendar_days(factor_names: list[str] = None) -> int:
    """因子所需最大日历日窗口。

    从 _PRICE_FN_MAP 提取每个因子的声明窗口, 取最大值,
    折算为日历日 (×1.5 覆盖周末节假日)。

    Args:
        factor_names: 因子名列表。None 或空列表 → 取全部已注册因子。

    Returns:
        日历日数。保证 ≥ 90 (60 交易日 × 1.5)。
    """
    names = factor_names if factor_names else list(_PRICE_FN_MAP.keys())
    max_td = 60
    for name in names:
        entry = _PRICE_FN_MAP.get(name)
        if entry is None:
            continue
        _, win = entry
        if isinstance(win, (int, float)) and win > 0:
            max_td = max(max_td, win)
    return int(max_td * 1.5)
