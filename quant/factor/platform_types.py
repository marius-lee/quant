from enum import Enum
from dataclasses import dataclass, field
from typing import List
from dataclasses import dataclass
"""Platform types."""

class FactorStatus(str, Enum):
    DRAFT = "draft"              # 草稿态
    PENDING_REVIEW = "pending"   # 待评审
    ACTIVE = "active"            # 生产可用
    DEPRECATED = "deprecated"    # 废弃
    ARCHIVED = "archived"        # 归档



class FactorCategory(str, Enum):
    PRICE = "price"           # 价量类
    FUNDAMENTAL = "fundamental"  # 基本面
    ALTERNATIVE = "alternative"  # 另类数据
    ML = "ml"                 # ML 生成
    COMPOSITE = "composite"   # 合成因子


@dataclass

class FactorTestCase:
    """因子测试用例."""
    name: str
    description: str
    input_data: Dict[str, Any]  # 输入数据 (symbols, dates, params)
    expected_output: Dict[str, Any]  # 期望输出 (shape, stats, values)
    tolerance: float = 1e-6
    tags: List[str] = field(default_factory=list)


@dataclass

class FactorPipelineResult:
    """流水线执行结果."""
    factor_name: str
    version: str
    stage: str  # compile, test, backtest, register, deploy
    status: str  # success, failed, skipped
    duration_sec: float
    details: Dict[str, Any] = field(default_factory=dict)
    error: str = ""



