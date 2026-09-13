import dagster as dg
from typing import Optional
"""Dagster assets types."""

class DataSourceRegistryResource(dg.ConfigurableResource):
    state_dir: str = "/tmp/quant_sources"
    state_dir_env: Optional[str] = None

    def __post_init__(self):
        if self.state_dir_env:
            self.state_dir = EnvVar(self.state_dir_env).get_value()

    def get_client(self):
        from quant.data.sources.registry import get_registry
        registry = get_registry()
        registry.load_from_config()
        return registry



class FactorStoreResource(dg.ConfigurableResource):
    db_path: str = "quant/data/factor_cache.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.factor.store import FactorStore
        return FactorStore(db_path=self.db_path)



class TradeRepoResource(dg.ConfigurableResource):
    db_path: str = "quant/data/trades.db"
    db_path_env: Optional[str] = None

    def __post_init__(self):
        if self.db_path_env:
            self.db_path = EnvVar(self.db_path_env).get_value()

    def get_client(self):
        from quant.data.repos import TradeRepo
        return TradeRepo(db_path=self.db_path)



