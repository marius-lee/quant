"""Data Repository Layer — typed DB access for all tables.

Usage:
    from data.repos import FactorRepo, UniverseRepo, TradeRepo, EvaluationRepo

    factor_repo = FactorRepo()
    factors = factor_repo.get_factors_by_status(("active", "monitoring"), name_list)
"""

from quant.data.repos._base import DatabaseManager
from quant.data.repos.factor_repo import FactorRepo
from quant.data.repos.universe_repo import UniverseRepo
from quant.data.repos.trade_repo import TradeRepo
from quant.data.repos.evaluation_repo import EvaluationRepo

__all__ = [
    "DatabaseManager",
    "FactorRepo",
    "UniverseRepo",
    "TradeRepo",
    "EvaluationRepo",
]
