"""分区策略 — 将因子物化工作拆分为并行任务.

设计:
  - DatePartitioner: 按日期分片 (最自然, 每日独立, 无数据竞争)
  - FactorPartitioner: 按因子分片 (适合因子间无依赖, 但需跨日期聚合)
  - SymbolPartitioner: 按股票分片 (适合横截面计算, 但需时序数据)
  - CompositePartitioner: 组合策略 (日期 × 因子 组合)

默认推荐: DatePartitioner + 因子批次内并行
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, List, Optional
from quant.execution.calendar import is_trading_day, get_trading_dates
from quant.utils.logger import get_logger

logger = get_logger("factor.distributed.partitioner")


@dataclass
class Partition:
    """单个分区 - 可独立计算的工作单元."""
    partition_id: str
    dates: List[str]           # 日期列表 (YYYY-MM-DD)
    factors: List[str]         # 因子列表
    symbols: List[str]         # 股票列表
    metadata: dict             # 扩展元数据

    def __len__(self) -> int:
        return len(self.dates) * len(self.factors) * len(self.symbols)

    def estimated_work(self) -> float:
        """估算工作量 (相对单位)."""
        return len(self.dates) * len(self.factors) * len(self.symbols) * 0.001


class BasePartitioner:
    """分区器基类."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        factors: List[str],
        symbols: List[str],
        max_partition_size: int = 50000,  # 单分区最大工作量
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.factors = factors
        self.symbols = symbols
        self.max_partition_size = max_partition_size

    def get_trading_dates(self) -> List[str]:
        """获取区间内所有交易日."""
        return get_trading_dates(self.start_date, self.end_date)

    def partition(self) -> List[Partition]:
        """生成分区列表."""
        raise NotImplementedError


class DatePartitioner(BasePartitioner):
    """按日期分区 — 每个交易日一个分区 (或多日合并).

    优点:
      - 日期间完全独立, 无数据竞争
      - 符合因子计算的时序特性 (每日需前几日数据, 但可预加载)
      - 易于增量计算 (仅新增日期)
      - 自然支持回填/重算单日

    缺点:
      - 单日数据量大时 (5000股 × 100因子) 单分区较重
      - 可通过 max_partition_size 控制合并多日
    """

    def partition(self) -> List[Partition]:
        trading_dates = self.get_trading_dates()
        if not trading_dates:
            logger.warning(f"No trading dates in range {self.start_date} - {self.end_date}")
            return []

        partitions = []
        current_batch: List[str] = []
        current_work = 0

        # 单日工作量估算
        daily_work = len(self.factors) * len(self.symbols)

        for d in trading_dates:
            if current_work + daily_work > self.max_partition_size and current_batch:
                # 创建分区
                partitions.append(Partition(
                    partition_id=f"date_batch_{len(partitions)}",
                    dates=current_batch.copy(),
                    factors=self.factors.copy(),
                    symbols=self.symbols.copy(),
                    metadata={"type": "date_batch", "start": current_batch[0], "end": current_batch[-1]},
                ))
                current_batch = []
                current_work = 0

            current_batch.append(d)
            current_work += daily_work

        # 最后一批
        if current_batch:
            partitions.append(Partition(
                partition_id=f"date_batch_{len(partitions)}",
                dates=current_batch,
                factors=self.factors.copy(),
                symbols=self.symbols.copy(),
                metadata={"type": "date_batch", "start": current_batch[0], "end": current_batch[-1]},
            ))

        logger.info(f"DatePartitioner: {len(trading_dates)} trading days → {len(partitions)} partitions "
                    f"(max_work={self.max_partition_size}, daily_work={daily_work})")
        return partitions


class FactorPartitioner(BasePartitioner):
    """按因子分区 — 每个因子 (或因子组) 一个分区.

    优点:
      - 因子间完全独立, 适合因子库扩展
      - 单因子全历史计算, 利于缓存复用

    缺点:
      - 需跨日期聚合结果
      - 单因子全历史工作量可能很大
    """

    def __init__(
        self,
        start_date: str,
        end_date: str,
        factors: List[str],
        symbols: List[str],
        max_partition_size: int = 50000,
        factors_per_partition: int = 10,
    ):
        super().__init__(start_date, end_date, factors, symbols, max_partition_size)
        self.factors_per_partition = factors_per_partition

    def partition(self) -> List[Partition]:
        trading_dates = self.get_trading_dates()
        if not trading_dates:
            return []

        partitions = []
        # 按因子分组
        for i in range(0, len(self.factors), self.factors_per_partition):
            factor_batch = self.factors[i:i + self.factors_per_partition]
            partitions.append(Partition(
                partition_id=f"factor_batch_{i // self.factors_per_partition}",
                dates=trading_dates,
                factors=factor_batch,
                symbols=self.symbols.copy(),
                metadata={"type": "factor_batch", "factor_indices": f"{i}-{i+len(factor_batch)-1}"},
            ))

        logger.info(f"FactorPartitioner: {len(self.factors)} factors → {len(partitions)} partitions "
                    f"({self.factors_per_partition} factors/partition)")
        return partitions


class SymbolPartitioner(BasePartitioner):
    """按股票分区 — 每个股票 (或股票组) 一个分区.

    优点:
      - 横截面计算天然并行
      - 适合无时序依赖的因子

    缺点:
      - 时序因子需跨分区共享历史数据 (数据传输开销大)
      - 结果聚合复杂
    """

    def __init__(
        self,
        start_date: str,
        end_date: str,
        factors: List[str],
        symbols: List[str],
        max_partition_size: int = 50000,
        symbols_per_partition: int = 500,
    ):
        super().__init__(start_date, end_date, factors, symbols, max_partition_size)
        self.symbols_per_partition = symbols_per_partition

    def partition(self) -> List[Partition]:
        trading_dates = self.get_trading_dates()
        if not trading_dates:
            return []

        partitions = []
        for i in range(0, len(self.symbols), self.symbols_per_partition):
            symbol_batch = self.symbols[i:i + self.symbols_per_partition]
            partitions.append(Partition(
                partition_id=f"symbol_batch_{i // self.symbols_per_partition}",
                dates=trading_dates,
                factors=self.factors.copy(),
                symbols=symbol_batch,
                metadata={"type": "symbol_batch", "symbol_indices": f"{i}-{i+len(symbol_batch)-1}"},
            ))

        logger.info(f"SymbolPartitioner: {len(self.symbols)} symbols → {len(partitions)} partitions "
                    f"({self.symbols_per_partition} symbols/partition)")
        return partitions


class CompositePartitioner(BasePartitioner):
    """组合分区 — 日期 × 因子 网格分区 (最灵活).

    生成 date × factor 网格, 每个网格为一个分区.
    适合大规模集群, 可精细控制并行度.
    """

    def __init__(
        self,
        start_date: str,
        end_date: str,
        factors: List[str],
        symbols: List[str],
        max_partition_size: int = 50000,
        dates_per_partition: int = 5,     # 每分区包含的交易日数
        factors_per_partition: int = 20,  # 每分区包含的因子数
    ):
        super().__init__(start_date, end_date, factors, symbols, max_partition_size)
        self.dates_per_partition = dates_per_partition
        self.factors_per_partition = factors_per_partition

    def partition(self) -> List[Partition]:
        trading_dates = self.get_trading_dates()
        if not trading_dates:
            return []

        partitions = []
        date_batches = [
            trading_dates[i:i + self.dates_per_partition]
            for i in range(0, len(trading_dates), self.dates_per_partition)
        ]
        factor_batches = [
            self.factors[i:i + self.factors_per_partition]
            for i in range(0, len(self.factors), self.factors_per_partition)
        ]

        for di, date_batch in enumerate(date_batches):
            for fi, factor_batch in enumerate(factor_batches):
                work = len(date_batch) * len(factor_batch) * len(self.symbols)
                if work > self.max_partition_size:
                    logger.warning(f"Partition {di}_{fi} work={work} exceeds max={self.max_partition_size}")

                partitions.append(Partition(
                    partition_id=f"composite_d{di}_f{fi}",
                    dates=date_batch,
                    factors=factor_batch,
                    symbols=self.symbols.copy(),
                    metadata={
                        "type": "composite",
                        "date_batch": di,
                        "factor_batch": fi,
                        "estimated_work": work,
                    },
                ))

        logger.info(f"CompositePartitioner: {len(trading_dates)} days × {len(self.factors)} factors "
                    f"→ {len(partitions)} partitions ({self.dates_per_partition}d × {self.factors_per_partition}f)")
        return partitions


def create_partitioner(
    strategy: str,
    start_date: str,
    end_date: str,
    factors: List[str],
    symbols: List[str],
    **kwargs
) -> BasePartitioner:
    """工厂函数."""
    strategies = {
        "date": DatePartitioner,
        "factor": FactorPartitioner,
        "symbol": SymbolPartitioner,
        "composite": CompositePartitioner,
    }
    if strategy not in strategies:
        raise ValueError(f"Unknown partition strategy: {strategy}. Available: {list(strategies.keys())}")
    return strategies[strategy](start_date, end_date, factors, symbols, **kwargs)