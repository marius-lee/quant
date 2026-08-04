"""test-v398: BacktestContext — 收敛 generate_signals 16+ 散装 kwargs 为单一上下文。

回测/实盘路径共用: 回测传预加载数据, 实盘传 None (回退 DB 查询)。
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from quant.config.paths import TRADE_DB


@dataclass
class BacktestContext:
    """回测上下文 — 所有预加载数据和复用实例的单一入口。

    回测路径: loop.py 创建并填充全部字段
    实盘路径: 传 None 或空实例, generate_signals 自动回退 DB 查询
    """

    # ── 预加载数据 ──
    data_full: Optional["pd.DataFrame"] = None       # MultiIndex (field, symbol)
    all_symbols: Optional[list] = None               # 全量股票列表

    # ── 基本面 PIT 组件 ──
    fund_stocks_df: Optional["pd.DataFrame"] = None   # stocks 表静态列
    fund_val_piv: Optional["pd.DataFrame"] = None     # daily_valuation pivot (date×symbol×field)
    fund_close_piv: Optional["pd.DataFrame"] = None   # close pivot (复用 data_full["close"])
    fund_high_52w: Optional["pd.DataFrame"] = None    # 52 周新高

    # ── 因子缓存 ──
    factor_cache: Optional[object] = None             # _FactorCache 实例
    factor_store: Optional[object] = None             # FactorStore 实例

    # ── 市场辅助数据 ──
    stock_names: Optional[dict] = None                # {symbol: name}
    preloaded_seal_ratios: Optional[dict] = None      # {date: [(symbol, lock_cap, amt)]}
    turnover_amount_roll: Optional["pd.DataFrame"] = None  # 成交额滚动均值
    bm_returns: Optional["pd.Series"] = None          # benchmark 日收益序列

    # ── 复用实例 (回测热路径消除每日 new) ──
    prebuilt_engine: Optional[object] = None          # ExecutionEngine
    prebuilt_cost_model: Optional[object] = None      # CostModel
    prebuilt_constructor: Optional[object] = None     # PortfolioConstructor

    # ── 回测配置 ──
    suppress_push: bool = True                        # 回测 = True, 实盘 = False
    db_path: str = TRADE_DB
    universe_size: Optional[int] = None
    ic_map: Optional[dict] = None
    combine_mode: Optional[str] = None
    regime_label: Optional[str] = None
    regime_probs: Optional[dict] = None

    # ── 回退兼容: 未传入实例时 generate_signals 自动 new ──
    def get_engine(self):
        if self.prebuilt_engine is not None:
            return self.prebuilt_engine
        from quant.execution.engine import ExecutionEngine
        return ExecutionEngine(db_path=self.db_path)

    def get_cost_model(self):
        if self.prebuilt_cost_model is not None:
            return self.prebuilt_cost_model
        from quant.execution.cost import CostModel
        return CostModel.from_config()

    def get_constructor(self):
        if self.prebuilt_constructor is not None:
            return self.prebuilt_constructor
        from quant.optimizer.portfolio import PortfolioConstructor
        return PortfolioConstructor()
