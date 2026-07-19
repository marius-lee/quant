"""Factor registry loaders and helpers — using Repository layer."""

from quant.data.repos import FactorRepo
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP


def _resolve_statuses(status_filter):
    """Convert status_filter string to tuple of statuses."""
    if status_filter is None:
        return None
    if isinstance(status_filter, (list, tuple)):
        return tuple(status_filter)
    if status_filter == 'using':
        # using = active + monitoring: monitoring 以衰减权重参与实盘信号生成
        # 来源: Grinold & Kahn (1999) Ch.6 Eq.6.16 — w_k ∝ IC_k/σ²_k
        #       监测因子权重由 |IC_5d| / |IC_60d| 连续比例决定, 无硬阈值
        #       QuantConnect 排除监测因子 (Inactive=removed),
        #       但 AQR/WorldQuant/Barra 保留监测因子在组合内仅降权
        #       — 本项目对齐机构级标准
        return ('active', 'monitoring')
    if status_filter == 'backtesting':
        # backtesting = 所有非 active/非 rejected 的因子
        # 来源: WorldQuant WebSim — 回测池包含注册/候选/退役/监测因子
        #       rejected 永久排除 (数据源死亡 或 retry_count ≥ max_retries)
        #       active 不参与回测 (已认证的线上因子)
        return ('registered', 'candidate', 'monitoring', 'retired')
    return (status_filter,)


def load_active_price_factors(status_filter='using'):
    """从 factor_registry 表加载价格因子 → {name: (cat, window, fn)}.

    status_filter:
        'using' → active + monitoring (实盘信号生成, monitoring 以衰减权重参与;
                   来源: Grinold & Kahn 1999 Ch.6 — w_k ∝ IC_k)
        'backtesting' → registered + candidate + monitoring + retired (回测评估池)
        None → 全部因子 (评估用)
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

    status_filter:
        'using' → active + monitoring (实盘信号生成, monitoring 以衰减权重参与;
                   来源: Grinold & Kahn 1999 Ch.6 — w_k ∝ IC_k)
        'backtesting' → registered + candidate + monitoring + retired (回测评估池)
        None → 全部因子 (评估用)
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
    from quant.factor.registry import _market_db_path
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

    status_filter:
        'using' → active + monitoring (实盘, monitoring 以衰减权重参与)
        'backtesting' → registered + candidate + monitoring + retired (回测池)
        'active' → 仅 active
        None → 全部因子
    """
    price_factors = load_active_price_factors(status_filter)
    fund_factors = load_active_fundamental_factors(status_filter)
    return list(price_factors.keys()) + list(fund_factors.keys())
