"""Factor registry loaders and helpers."""

from factor.registry import _db_connect
from factor.compute._shared import _market_db_path
from data.store import market_conn as _market_conn
from factor.compute.price import _PRICE_FN_MAP
from factor.compute.fundamental import _FUNDAMENTAL_FN_MAP

def load_active_price_factors(status_filter='using'):
    """从 factor_registry 表加载价格因子 → {name: (cat, window, fn)}.
    
    status_filter: 'using'→active+monitoring (生产中), None (全部, 评估用).
    """
    conn = _db_connect()
    name_list = list(_PRICE_FN_MAP.keys())
    placeholders = ",".join("?" * len(name_list))
    if status_filter:
        if isinstance(status_filter, (list, tuple)):
            statuses = tuple(status_filter)
        elif status_filter == 'using':
            statuses = ('active', 'monitoring')
        elif status_filter == 'backtesting':
            statuses = ('registered', 'candidate', 'retired', 'rejected')
        else:
            statuses = (status_filter,)
        ph = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE status IN ({ph}) AND name IN ({placeholders})",
            list(statuses) + name_list
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE name IN ({placeholders})",
            name_list
        ).fetchall()
    conn.close()
    result = {}
    for (name,) in rows:
        if name in _PRICE_FN_MAP:
            fn, win = _PRICE_FN_MAP[name]
            result[name] = ("dynamic", win, fn)
    return result

def load_active_fundamental_factors(status_filter='using'):
    """从 factor_registry 表加载基本面因子.
    
    status_filter: 'using'→active+monitoring (生产中), None (全部, 评估用).
    """
    conn = _db_connect()
    fn_names = list(_FUNDAMENTAL_FN_MAP.keys())
    placeholders = ",".join("?" * len(fn_names))
    if status_filter:
        if isinstance(status_filter, (list, tuple)):
            statuses = tuple(status_filter)
        elif status_filter == 'using':
            statuses = ('active', 'monitoring')
        elif status_filter == 'backtesting':
            statuses = ('registered', 'candidate', 'retired', 'rejected')
        else:
            statuses = (status_filter,)
        ph = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE status IN ({ph}) AND name IN ({placeholders})",
            list(statuses) + fn_names
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT name FROM factor_registry WHERE name IN ({placeholders})",
            fn_names
        ).fetchall()
    conn.close()
    result = {}
    for (name,) in rows:
        if name in _FUNDAMENTAL_FN_MAP:
            cat, fn = _FUNDAMENTAL_FN_MAP[name]
            result[name] = (cat, fn)
    return result

def update_factor_evaluation(name: str, ic_mean: float, ic_ir: float):
    """回测后更新因子 IC 到数据库."""
    conn = _market_conn("rw")
    conn.execute(
        "UPDATE factor_registry SET ic_mean=?, ic_ir=?, last_evaluated=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE name=?",
        (round(ic_mean, 6), round(ic_ir, 4), name)
    )
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════════════


def get_factor_names(status_filter=None) -> list:
    """返回因子名列表 (从 factor_registry 表读取)。

    status_filter: 'active' (生产), None (全部, 评估用).
    """
    price_factors = load_active_price_factors(status_filter)
    fund_factors = load_active_fundamental_factors(status_filter)
    return list(price_factors.keys()) + list(fund_factors.keys())



