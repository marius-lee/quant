"""FactorStateManager — 因子状态机 (单一真相源).

向后兼容层: 重新导出 state_machine.FactorStateMachine 为 FactorStateManager.

所有模块通过此管理器转换因子状态，不直接写 factor_registry.status。
对标: WorldQuant 单一调度器 + QuantConnect 事件驱动状态机。

设计约束:
  - 零 fallback: 非法转换 → ValueError
  - 所有阈值从 config.yaml 读取
  - 状态转换表为纯数据结构，可单元测试
"""

from quant.factor.state_machine import (
    FactorStateMachine as FactorStateManager,
    InvalidTransitionError,
    FactorEvent,
)

# 导出原有接口
__all__ = [
    "FactorStateManager",
    "InvalidTransitionError",
    "FactorEvent",
]