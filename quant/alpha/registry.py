"""Alpha Model Registry (P2-6) — 中央化模型注册与工厂.

避免: 硬编码 model_path / 手工导入 / 单例全局变量.
提供: @register_alpha 装饰器 + get_alpha_model() 工厂 + 列表查询.

用法:
    from quant.alpha.registry import register_alpha, get_alpha_model, list_alphas

    @register_alpha("lgb_v307")
    class LgbAlphaModel:
        ...

    model = get_alpha_model("lgb_v307")()
"""

from typing import Any, Callable, Optional
from quant.utils.logger import get_logger

logger = get_logger("alpha.registry")

# 全局注册表: name -> factory (class or function returning model instance)
_MODEL_REGISTRY: dict[str, Callable[[], Any]] = {}

# 元数据: name -> {"class": cls, "description": str, "version": str, ...}
_METADATA: dict[str, dict] = {}


def register_alpha(
    name: str,
    description: str = "",
    version: str = "1.0",
    **metadata
) -> Callable[[type], type]:
    """装饰器: 注册 Alpha 模型类.

    Args:
        name: 模型唯一标识 (如 "lgb_v307", "xgb_v307", "sleeve_ic")
        description: 简短描述
        version: 语义化版本
        **metadata: 任意额外元数据

    Returns:
        装饰器, 不修改原类, 只注册.
    """
    def _decorator(cls: type) -> type:
        if name in _MODEL_REGISTRY:
            logger.warning(f"alpha.registry: overwriting existing model '{name}'")
        _MODEL_REGISTRY[name] = cls
        _METADATA[name] = {
            "class": cls,
            "description": description,
            "version": version,
            **metadata,
        }
        logger.info(f"alpha.registry: registered '{name}' ({cls.__name__})")
        return cls
    return _decorator


def get_alpha_model(name: str) -> Callable[[], Any]:
    """获取模型工厂函数 (可直接实例化).

    Args:
        name: 已注册的模型名

    Returns:
        类或工厂函数, 调用返回实例

    Raises:
        KeyError: 模型未注册
    """
    if name not in _MODEL_REGISTRY:
        available = ", ".join(sorted(_MODEL_REGISTRY.keys()))
        raise KeyError(f"Alpha model '{name}' not registered. Available: {available}")
    return _MODEL_REGISTRY[name]


def list_alphas() -> dict[str, dict]:
    """列出所有已注册模型及元数据."""
    return dict(_METADATA)


def is_registered(name: str) -> bool:
    """检查模型是否已注册."""
    return name in _MODEL_REGISTRY


# ── 向后兼容: 自动注册现有模型 (import 时触发) ──
# 用户代码无需显式 import registry, 只需 import quant.alpha.qlib_model / xgb_model
# 这些模块在 import 时会自动调用 register_alpha().

# LGB 模型注册 (导入 qlib_model 时自动注册)
try:
    from quant.alpha.qlib_model import LgbAlphaModel
    register_alpha(
        "lgb",
        description="LightGBM-based Alpha Model (v307+)",
        version="3.0",
        source="qlib_model",
    )(LgbAlphaModel)
except ImportError:
    pass

# XGB 模型注册 (导入 xgb_model 时自动注册)
try:
    from quant.alpha.xgb_model import XgbAlphaModel
    register_alpha(
        "xgb",
        description="XGBoost-based Alpha Model",
        version="1.0",
        source="xgb_model",
    )(XgbAlphaModel)
except ImportError:
    pass

# 基础合成模型注册 (无需 ML, 纯因子合成)
try:
    from quant.alpha.synth import ic_weighted, equal_weight, intersection_alpha
    register_alpha(
        "ic_weighted",
        description="IC-weighted factor synthesis",
        version="1.0",
        source="synth",
        function=True,
    )(lambda: ic_weighted)
    register_alpha(
        "equal_weight",
        description="Equal-weight factor synthesis",
        version="1.0",
        source="synth",
        function=True,
    )(lambda: equal_weight)
    register_alpha(
        "intersection",
        description="Intersection-based factor synthesis",
        version="1.0",
        source="synth",
        function=True,
    )(lambda: intersection_alpha)
except ImportError:
    pass