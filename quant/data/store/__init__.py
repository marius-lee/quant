"""SQLite 数据仓库 — 全A股 + 增量更新 (v435: 读查询分流到 DuckDB).

DataStore 通过多继承组合各 mixin，保持接口不变。
"""
from quant.data.store._core import DataStoreCoreMixin, D_DATE, D_SYMBOL, D_OPEN, D_HIGH, D_LOW, D_CLOSE, D_VOLUME, D_AMOUNT, D_TURNOVER, D_PE_TTM, D_PB, D_TOTAL_MV, D_CIRC_MV, S_SYMBOL, S_NAME, S_MARKET, S_LIST_DATE, S_INDUSTRY, F_PE, F_PB, F_TOTAL_MV, F_CIRC_MV, F_ROE, F_EPS, F_BVPS
from quant.data.store._fetch import DataStoreFetchMixin
from quant.data.store._sync import DataStoreSyncMixin
from quant.data.store._sync2 import DataStoreSync2Mixin
from quant.data.store._backfill import DataStoreBackfillMixin
from quant.data.store._backfill2 import DataStoreBackfill2Mixin
from quant.data.store._query import DataStoreQueryMixin
from quant.data.store._helpers import DataStoreHelperMixin, market_conn, _ts_code, _bs_socket_timeout, _tencent_market, logger, _TICKFLOW_BATCH_NO_PERM


class DataStore(DataStoreCoreMixin, DataStoreFetchMixin, DataStoreSyncMixin, DataStoreSync2Mixin,
                  DataStoreBackfillMixin, DataStoreBackfill2Mixin, DataStoreQueryMixin, DataStoreHelperMixin):
    """全A股 SQLite 数据仓库 — 单连接复用，任务结束时关闭。

    v435: 读查询分流到 DuckDB (列式并行)，写入仍走 SQLite 事务。
    """
    pass


__all__ = ['DataStore', 'market_conn', 'logger', '_TICKFLOW_BATCH_NO_PERM',
           'D_DATE', 'D_SYMBOL', 'D_OPEN', 'D_HIGH', 'D_LOW', 'D_CLOSE',
           'D_VOLUME', 'D_AMOUNT', 'D_TURNOVER', 'D_PE_TTM', 'D_PB',
           'D_TOTAL_MV', 'D_CIRC_MV', 'S_SYMBOL', 'S_NAME', 'S_MARKET',
           'S_LIST_DATE', 'S_INDUSTRY', 'F_PE', 'F_PB', 'F_TOTAL_MV',
           'F_CIRC_MV', 'F_ROE', 'F_EPS', 'F_BVPS', '_ts_code',
           '_bs_socket_timeout', '_tencent_market', 'DataStoreHelperMixin']