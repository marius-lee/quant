"""ExecutionContext — 统一回测/实盘上下文，消除 20+ 散装 kwargs。

BacktestContext 与 PipelineContext 合并为单一 ExecutionContext，
生成信号 / 执行交易 / 回测 / 实盘全流程共用。
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from quant.config.paths import TRADE_DB


@dataclass
class ExecutionContext:
    """统一执行上下文 — 所有预加载数据、复用实例、配置参数的单一入口。

    回测路径: loop.py 创建并填充全部字段
    实盘路径: orchestrator/execute.py 创建空实例，generate_signals 自动回退 DB 查询
    """

    # ── 预加载数据 ──
    data_full: Optional["pd.DataFrame"] = None       # MultiIndex (field, symbol)
    all_symbols: Optional[list] = None               # 全量股票列表

    # ── 基本面 PIT 组件 ──
    fund_stocks_df: Optional["pd.DataFrame"] = None   # stocks 表静态列
    fund_val_piv: Optional["pd.DataFrame"] = None     # daily_valuation pivot (date×symbol×field)
    fund_close_piv: Optional["pd.DataFrame"] = None   # close pivot (复用 data_full["close"])
    fund_high_52w: Optional["pd.DataFrame"] = None    # 52 周新高
    industry_piv: Optional["pd.DataFrame"] = None     # industry_history PIT pivot (date×symbol industry) v502

    # ── 因子缓存 ──
    factor_cache: Optional[object] = None             # _FactorCache 实例
    factor_store: Optional[object] = None             # FactorStore 实例

    # ── 回测热路径注入 (test-v466) ──
    atr_panel: Optional[dict] = None                  # {date: {symbol: atr}} — ATR 止损免逐仓 SQL
    probation_names: Optional[list] = None            # 回测启动时冻结的 probation 因子名单 (PIT)

    # ── 市场辅助数据 ──
    stock_names: Optional[dict] = None                # {symbol: name}
    preloaded_seal_ratios: Optional[dict] = None      # {date: [(symbol, lock_cap, amt)]}
    turnover_amount_roll: Optional["pd.DataFrame"] = None  # 成交额滚动均值
    bm_returns: Optional["pd.Series"] = None          # benchmark 日收益序列

    # ── 复用实例 (回测/实盘热路径消除每日 new) ──
    prebuilt_engine: Optional[object] = None          # ExecutionEngine
    prebuilt_cost_model: Optional[object] = None      # CostModel
    prebuilt_constructor: Optional[object] = None     # PortfolioConstructor

    # ── 运行配置 ──
    suppress_push: bool = True                        # 回测 = True, 实盘 = False
    db_path: str = TRADE_DB
    universe_size: Optional[int] = None
    ic_map: Optional[dict] = None
    combine_mode: Optional[str] = None
    regime_label: Optional[str] = None
    regime_probs: Optional[dict] = None

    # ── 实盘特有 ──
    live_broker_adapter: Optional[object] = None      # BrokerAdapter 实例 (实盘执行用)
    ohlc: Optional[dict] = None                       # B8: 回测一字板判定

    # ── 回退兼容: 未传入实例时 generate_signals/execute_signals 自动 new ──
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