"""PipelineContext — 依赖注入容器，减少 generate_signals() 参数膨胀。

A1/A3: 替代 16 个独立参数，将可复用依赖集中注入。
回测/实盘共享同一个 ctx 实例，单点初始化。
"""
from quant.config.paths import TRADE_DB
from dataclasses import dataclass, field


@dataclass
class PipelineContext:
    """Pipeline 执行上下文 — 可复用依赖的容器。

    所有字段均为可选，未提供时在 generate_signals() 内按需创建默认实例。
    """

    # ── 数据源 ──
    store: object = None          # DataStore 实例
    factor_store: object = None   # FactorStore 实例（缓存）

    # ── 执行层 ──
    engine: object = None         # ExecutionEngine
    cost_model: object = None     # CostModel
    constructor: object = None    # PortfolioConstructor

    # ── 预计算数据（回测优化） ──
    preloaded_data: object = None  # pd.DataFrame，预加载的全量行情
    primitives: dict = field(default_factory=dict)  # 预计算算子
    ic_map: dict | None = None    # 预加载的 IC 权重
    factor_values: dict | None = None  # 预加载的因子值

    # ── 路径/参数 ──
    db_path: str = TRADE_DB
    suppress_push: bool = False   # 回测用，不推送到 Web
