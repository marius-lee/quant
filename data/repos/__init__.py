"""Data Repository Layer — typed DB access for all tables.

Usage:
    from data.repos import FactorRepo, UniverseRepo, TradeRepo, EvaluationRepo

    factor_repo = FactorRepo()
    factors = factor_repo.get_factors_by_status(("active", "monitoring"), name_list)
"""

from data.repos._base import DatabaseManager
from data.repos.factor_repo import FactorRepo
from data.repos.universe_repo import UniverseRepo
from data.repos.trade_repo import TradeRepo
from data.repos.evaluation_repo import EvaluationRepo

__all__ = [
    "DatabaseManager",
    "FactorRepo",
    "UniverseRepo",
    "TradeRepo",
    "EvaluationRepo",
]
