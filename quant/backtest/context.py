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


class LiveContext:
    """test-v398: 实盘上下文 — 轻量 BacktestContext 工厂, 每日复用实例.

    与 BacktestContext 的区别:
      - 不做数据预加载 (实盘需数据新鲜度)
      - 只复用 engine/cost_model/constructor (避免每日 DDL)
      - 通过 to_backtest_context() 转为 BacktestContext 传给 generate_signals

    用法:
        ctx = LiveContext(db_path=TRADE_DB)
        result = generate_signals(..., ctx=ctx.to_backtest_context())
    """

    __slots__ = ("db_path", "_engine", "_cost_model", "_constructor")

    def __init__(self, db_path: str = TRADE_DB):
        self.db_path = db_path
        self._engine = None
        self._cost_model = None
        self._constructor = None

    def to_backtest_context(self) -> "BacktestContext":
        """转为 BacktestContext — 只填复用实例, 数据字段全部留 None (回退 DB 查询)."""
        return BacktestContext(
            prebuilt_engine=self.engine,
            prebuilt_cost_model=self.cost_model,
            prebuilt_constructor=self.constructor,
            suppress_push=False,
            db_path=self.db_path,
        )

    @property
    def engine(self):
        if self._engine is None:
            from quant.execution.engine import ExecutionEngine
            self._engine = ExecutionEngine(db_path=self.db_path)
        return self._engine

    @property
    def cost_model(self):
        if self._cost_model is None:
            from quant.execution.cost import CostModel
            self._cost_model = CostModel.from_config()
        return self._cost_model

    @property
    def constructor(self):
        if self._constructor is None:
            from quant.optimizer.portfolio import PortfolioConstructor
            self._constructor = PortfolioConstructor()
        return self._constructor
