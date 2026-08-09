"""Alpha Strategy Registry — 统一 Alpha 合成策略的注册、发现、实例化。

设计目标:
  1. 替代 AlphaModel.combine() 中的字符串分支 (if/elif combine_mode)
  2. 新增策略零侵入: 只需装饰器 @register_alpha("name")
  3. 支持参数化构造: AlphaStrategy.create("sleeve", positions_per_factor=10)
  4. 元数据自动收集: list_alphas() 返回所有已注册策略的元数据

用法:
    from quant.alpha.strategy import register_alpha, get_alpha, list_alphas

    @register_alpha("my_sleeve", description="自定义分仓策略", version="1.0")
    class MySleeveAlpha(AlphaStrategy):
        def combine(self, factor_values, ic_map, **params):
            ...

    strategy = get_alpha("sleeve")()
    alpha = strategy.combine(factor_values, ic_map)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from quant.utils.logger import get_logger

logger = get_logger("alpha.strategy")


@dataclass
class AlphaStrategyMeta:
    """策略元数据 — 注册时自动收集。"""
    name: str
    description: str = ""
    version: str = "1.0"
    params: dict = field(default_factory=dict)  # 参数默认值
    category: str = "composite"  # composite | sleeve | ml | custom


class AlphaStrategy(ABC):
    """Alpha 合成策略抽象基类。

    子类需实现 combine() 方法，接受 factor_values, ic_map 和可选参数。

    参数规范:
      - factor_values: {name: Series(index=symbol)} — 同日期截面因子值
      - ic_map: {name: weight} — IC 权重 (仅 ic_weighted 等模式使用)
      - **params: 策略特定参数 (positions_per_factor, top_fraction 等)

    返回: Series(index=symbol), 合成 alpha 得分
    """

    # 类级元数据 (子类可重写)
    _meta = AlphaStrategyMeta(name="base", description="基础抽象策略")

    @property
    def meta(self) -> AlphaStrategyMeta:
        return self._meta

    @abstractmethod
    def combine(
        self,
        factor_values: dict,
        ic_map: Optional[dict] = None,
        **params,
    ) -> "pd.Series":
        """执行 Alpha 合成，返回 Series(index=symbol)。"""
        ...

    def __init_subclass__(cls, **kwargs):
        # 子类定义时自动注册 (如果定义了 _meta.name)
        super().__init_subclass__(**kwargs)
        if hasattr(cls, '_meta') and cls._meta.name != "base":
            register_alpha(cls._meta.name, cls, cls._meta.description, cls._meta.version,
                          cls._meta.category, **cls._meta.params)


# ── 全局注册表 ──
_ALPHA_REGISTRY: dict[str, type[AlphaStrategy]] = {}
_META_REGISTRY: dict[str, AlphaStrategyMeta] = {}


def register_alpha(
    name: str,
    cls: Optional[type[AlphaStrategy]] = None,
    description: str = "",
    version: str = "1.0",
    category: str = "composite",
    **params,
) -> callable:
    """装饰器: 注册 Alpha 策略类。

    用法:
        @register_alpha("my_strategy", description="自定义策略")
        class MyAlpha(AlphaStrategy):
            def combine(self, factor_values, ic_map, **params): ...

    或作为函数调用:
        register_alpha("my_strategy", MyAlphaClass, description="...")
    """
    def _decorator(cls: type[AlphaStrategy]) -> type[AlphaStrategy]:
        if name in _ALPHA_REGISTRY:
            logger.warning(f"alpha.strategy: overwriting existing strategy '{name}'")
        cls._meta = AlphaStrategyMeta(
            name=name,
            description=description,
            version=version,
            params=params,
            category=category,
        )
        _ALPHA_REGISTRY[name] = cls
        logger.info(f"alpha.strategy: registered '{name}' ({cls.__name__})")
        return cls

    if cls is None:
        return _decorator
    return _decorator(cls)


def get_alpha(name: str) -> type[AlphaStrategy]:
    """获取策略类 (工厂函数)。

    Args:
        name: 已注册的策略名

    Returns:
        策略类 (可直接实例化)

    Raises:
        KeyError: 策略未注册
    """
    if name not in _ALPHA_REGISTRY:
        available = ", ".join(sorted(_ALPHA_REGISTRY.keys()))
        raise KeyError(f"Alpha strategy '{name}' not registered. Available: {available}")
    return _ALPHA_REGISTRY[name]


def list_alphas() -> dict[str, AlphaStrategyMeta]:
    """列出所有已注册策略及其元数据。"""
    return dict(_META_REGISTRY)


def is_registered(name: str) -> bool:
    """检查策略是否已注册。"""
    return name in _ALPHA_REGISTRY


# ── 向后兼容: 自动注册现有合成函数 (import 时触发) ──
# 这些函数通过包装器适配为 AlphaStrategy 子类
def _wrap_sleeve_compose() -> type[AlphaStrategy]:
    """包装 sleeve_compose 函数为 AlphaStrategy 子类。"""
    from quant.alpha.synth import sleeve_compose

    @register_alpha(
        "sleeve",
        description="分仓合成: 每因子独立选 top-N 取并集",
        version="1.0",
        category="sleeve",
        positions_per_factor=10,
        min_factors=3,
    )
    class _SleeveAlpha:
        def combine(self, factor_values, ic_map=None, **params):
            positions_per_factor = params.get("positions_per_factor", 10)
            min_factors = params.get("min_factors", 3)
            return sleeve_compose(factor_values, positions_per_factor, min_factors)

    return _SleeveAlpha


def _wrap_ic_weighted() -> type[AlphaStrategy]:
    """包装 ic_weighted 函数为 AlphaStrategy 子类。"""
    from quant.alpha.synth import ic_weighted

    @register_alpha(
        "ic_weighted",
        description="IC 加权合成: 权重 ∝ |IC|",
        version="1.0",
        category="composite",
        clip=3.0,
    )
    class _ICWeightedAlpha:
        def combine(self, factor_values, ic_map=None, **params):
            clip = params.get("clip", 3.0)
            return ic_weighted(factor_values, ic_map or {}, clip)

    return _ICWeightedAlpha


def _wrap_equal_weight() -> type[AlphaStrategy]:
    """包装 equal_weight 函数为 AlphaStrategy 子类。"""
    from quant.alpha.synth import equal_weight

    @register_alpha(
        "equal_weight",
        description="等权合成: 所有因子 z-score 后等权平均",
        version="1.0",
        category="composite",
    )
    class _EqualWeightAlpha:
        def combine(self, factor_values, ic_map=None, **params):
            return equal_weight(factor_values)

    return _EqualWeightAlpha


def _wrap_intersection_alpha() -> type[AlphaStrategy]:
    """包装 intersection_alpha 函数为 AlphaStrategy 子类。"""
    from quant.alpha.synth import intersection_alpha

    @register_alpha(
        "intersection",
        description="交集筛选: 每因子排前 X% 才进候选池",
        version="1.0",
        category="composite",
        top_fraction=0.2,
        primary_factor="gap_5d",
    )
    class _IntersectionAlpha:
        def combine(self, factor_values, ic_map=None, **params):
            top_fraction = params.get("top_fraction", 0.2)
            primary_factor = params.get("primary_factor", "gap_5d")
            return intersection_alpha(factor_values, top_fraction, primary_factor)

    return _IntersectionAlpha


def _wrap_strict_intersection() -> type[AlphaStrategy]:
    """包装 strict_intersection 函数为 AlphaStrategy 子类。"""
    from quant.alpha.synth import strict_intersection

    @register_alpha(
        "strict_intersection",
        description="严格交集: 每因子取 top N, 必须同时出现",
        version="1.0",
        category="composite",
        top_n_per_factor=100,
        primary_factor="gap_5d",
    )
    class _StrictIntersectionAlpha:
        def combine(self, factor_values, ic_map=None, **params):
            top_n_per_factor = params.get("top_n_per_factor", 100)
            primary_factor = params.get("primary_factor", "gap_5d")
            return strict_intersection(factor_values, top_n_per_factor, primary_factor)

    return _StrictIntersectionAlpha


# 自动注册所有内置策略 (import 时触发)
_wrap_sleeve_compose()
_wrap_ic_weighted()
_wrap_equal_weight()
_wrap_intersection_alpha()
_wrap_strict_intersection()


# ── ML 模型策略注册 (延迟导入, 避免循环依赖) ──
def _register_ml_models():
    """注册 ML 模型策略 (延迟导入)。"""
    try:
        from quant.alpha.qlib_model import LgbAlphaModel
        register_alpha(
            "lgb",
            description="LightGBM-based Alpha Model (v307+)",
            version="3.0",
            category="ml",
        )(LgbAlphaModel)
    except ImportError:
        pass

    try:
        from quant.alpha.xgb_model import XgbAlphaModel
        register_alpha(
            "xgb",
            description="XGBoost-based Alpha Model",
            version="1.0",
            category="ml",
        )(XgbAlphaModel)
    except ImportError:
        pass


_register_ml_models()