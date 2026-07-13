"""Factor registry loaders and helpers — using Repository layer."""

from data.repos import FactorRepo
from factor.compute.price import _PRICE_FN_MAP
from factor.compute.fundamental import _FUNDAMENTAL_FN_MAP


def _resolve_statuses(status_filter):
    """Convert status_filter string to tuple of statuses."""
    if status_filter is None:
        return None
    if isinstance(status_filter, (list, tuple)):
        return tuple(status_filter)
    if status_filter == 'using':
        return ('active', 'monitoring')
    if status_filter == 'backtesting':
        return ('registered', 'candidate', 'retired')
    return (status_filter,)


def load_active_price_factors(status_filter='using'):
    """从 factor_registry 表加载价格因子 → {name: (cat, window, fn)}.

    status_filter: 'using'→active+monitoring (生产中), None (全部, 评估用).
    """
    statuses = _resolve_statuses(status_filter)
    name_list = list(_PRICE_FN_MAP.keys())
    repo = FactorRepo()
    active_names = {f["name"] for f in repo.get_factors_by_status(statuses, name_list)} if statuses else set(name_list)
    result = {}
    for name, (fn, win) in _PRICE_FN_MAP.items():
        if name in active_names:
            result[name] = ("dynamic", win, fn)
    return result


def load_active_fundamental_factors(status_filter='using'):
    """从 factor_registry 表加载基本面因子.

    status_filter: 'using'→active+monitoring (生产中), None (全部, 评估用).
    """
    statuses = _resolve_statuses(status_filter)
    fn_names = list(_FUNDAMENTAL_FN_MAP.keys())
    repo = FactorRepo()
    active_names = {f["name"] for f in repo.get_factors_by_status(statuses, fn_names)} if statuses else set(fn_names)
    result = {}
    for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
        if name in active_names:
            result[name] = (cat, fn)
    return result


def update_factor_evaluation(name: str, ic_mean: float, ic_ir: float):
    """回测后更新因子 IC 到数据库."""
    import sqlite3
    from factor.registry import _market_db_path
    conn = sqlite3.connect(_market_db_path())
    conn.execute(
        "UPDATE factor_registry SET ic_mean=?, ic_ir=?, last_evaluated=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE name=?",
        (round(ic_mean, 6), round(ic_ir, 4), name)
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════


def get_factor_names(status_filter=None) -> list:
    """返回因子名列表 (从 factor_registry 表读取).

    status_filter: 'active' (生产), None (全部, 评估用).
    """
    price_factors = load_active_price_factors(status_filter)
    fund_factors = load_active_fundamental_factors(status_filter)
    return list(price_factors.keys()) + list(fund_factors.keys())
