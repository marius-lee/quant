"""因子计算结果聚合器."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

@dataclass
class ComputeResult:
    """单个分区计算结果."""
    partition_id: str
    success: bool
    rows_written: int = 0
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class FactorResultAggregator:
    """因子计算结果聚合器.

    职责:
      - 收集各分区结果
      - 统计总行数、耗时、失败分区
      - 生成汇总报告
    """

    def __init__(self):
        self.results: List[ComputeResult] = []

    def add_result(self, result: ComputeResult):
        self.results.append(result)

    def get_summary(self) -> Dict[str, Any]:
        total_rows = sum(r.rows_written for r in self.results)
        total_elapsed = sum(r.elapsed_ms for r in self.results)
        failed = [r for r in self.results if not r.success]
        succeeded = [r for r in self.results if r.success]

        return {
            "total_partitions": len(self.results),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "total_rows_written": total_rows,
            "total_elapsed_ms": total_elapsed,
            "avg_elapsed_ms": total_elapsed / len(self.results) if self.results else 0,
            "failed_partitions": [r.partition_id for r in failed],
            "success_rate": len(succeeded) / len(self.results) if self.results else 0,
        }

    def all_succeeded(self) -> bool:
        return all(r.success for r in self.results)