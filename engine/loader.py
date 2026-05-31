"""因子加载 + 股票筛选 + 价格读取"""
from data.store import DataStore
from data.repository import StockRepo, PriceRepo
from utils.logger import get_logger

logger = get_logger("engine.loader")


def get_training_data(store: DataStore) -> dict:
    """加载股票列表和收盘价，不做全量因子加载（由 screener/trainer 分块处理）。

    返回 {stocks, close_df, all_stocks}
    """
    stocks_repo = StockRepo(store)
    prices_repo = PriceRepo(store)

    all_stocks = stocks_repo.get_qualified()
    if len(all_stocks) < 5:
        return {"error": "数据不足", "all_stocks": all_stocks}

    close_df = prices_repo.get_close(all_stocks)
    if close_df.empty:
        return {"error": "无训练数据", "all_stocks": all_stocks}

    logger.info(f"loaded {len(all_stocks)} stocks, {len(close_df)} days")
    return {
        "all_stocks": all_stocks,
        "close_df": close_df,
    }
