#!/usr/bin/env python3
"""分布式因子物化性能基准测试.

用法:
    python scripts/benchmark_factorization.py --mode full --factors 104 --symbols 5000 --days 1000
    python scripts/benchmark_factorization.py --mode incremental --days 1
    python scripts/benchmark_factorization.py --mode distributed --actors 8 --cpus 32
"""

import argparse
import time
import json
import sys
from datetime import date, timedelta
from typing import Dict, Any, List
from dataclasses import dataclass, asdict

# Add project root to path
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.factor.distributed.engine import DistributedFactorEngine
from quant.factor.distributed.ray_config import init_ray, shutdown_ray


@dataclass
class BenchmarkResult:
    """基准测试结果."""
    mode: str
    start_date: str
    end_date: str
    num_factors: int
    num_symbols: int
    num_dates: int
    total_work_units: int
    duration_seconds: float
    rows_written: int
    partitions: int
    actor_pool_used: bool
    actor_pool_size: int
    incremental: bool
    memory_peak_mb: float
    timestamp: str


def get_test_config(mode: str) -> Dict[str, Any]:
    """获取测试配置."""
    configs = {
        "full": {
            "start_date": "2020-01-01",
            "end_date": "2024-12-31",
            "factors": None,  # 自动发现
            "symbols": None,  # 自动全市场
            "partition_strategy": "composite",
            "partition_kwargs": {"max_partition_size": 50000, "dates_per_partition": 5, "factors_per_partition": 20},
        },
        "incremental": {
            "start_date": (date.today() - timedelta(days=30)).strftime("%Y-%m-%d"),
            "end_date": date.today().strftime("%Y-%m-%d"),
            "factors": None,
            "symbols": None,
            "partition_strategy": "date",
            "partition_kwargs": {"max_partition_size": 50000},
            "incremental": True,
        },
        "distributed": {
            "start_date": "2023-01-01",
            "end_date": "2024-12-31",
            "factors": None,
            "symbols": None,
            "partition_strategy": "composite",
            "partition_kwargs": {"max_partition_size": 50000, "dates_per_partition": 5, "factors_per_partition": 20},
            "use_actor_pool": True,
            "actor_pool_size": 8,
        },
        "single_date": {
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
            "factors": None,
            "symbols": None,
            "partition_strategy": "date",
            "partition_kwargs": {"max_partition_size": 50000},
        },
    }
    return configs.get(mode, configs["full"])


def run_benchmark(config: Dict[str, Any], mode: str) -> BenchmarkResult:
    """运行基准测试."""
    print(f"\n{'='*60}")
    print(f"Benchmark: {mode.upper()}")
    print(f"{'='*60}")
    
    start_date = config["start_date"]
    end_date = config["end_date"]
    factors = config.get("factors")
    symbols = config.get("symbols")
    partition_strategy = config.get("partition_strategy", "date")
    partition_kwargs = config.get("partition_kwargs", {})
    ray_config = config.get("ray_config", {})
    max_concurrent_tasks = config.get("max_concurrent_tasks", 0)
    use_actor_pool = config.get("use_actor_pool", False)
    actor_pool_size = config.get("actor_pool_size", 0)
    incremental = config.get("incremental", False)

    # 计算工作量
    from quant.factor.compute import get_factor_names
    from quant.data.repos.universe_repo import UniverseRepo
    from quant.execution.calendar import get_trading_dates
    
    factors = factors or sorted(set(
        get_factor_names(status_filter='backtesting')
    ) | set(get_factor_names(status_filter='using')))
    symbols = symbols or UniverseRepo().get_symbols(exclude_market='BJ')
    dates = get_trading_dates(config["start_date"], config["end_date"])
    
    num_factors = len(factors)
    num_symbols = len(symbols)
    num_dates = len(dates)
    total_work = num_factors * num_symbols * num_dates

    print(f"Config: {mode}")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Factors: {num_factors}")
    print(f"  Symbols: {num_symbols}")
    print(f"  Dates: {num_dates}")
    print(f"  Total work units: {total_work:,}")
    print(f"  Partition strategy: {partition_strategy}")
    print(f"  Incremental: {incremental}")
    print(f"  Actor pool: {use_actor_pool} (size={actor_pool_size})")
    print(f"  Max concurrent: {max_concurrent_tasks}")

    # 启动 Ray
    init_ray(ray_config)
    
    # 创建引擎
    engine = DistributedFactorEngine(
        start_date=start_date,
        end_date=end_date,
        factors=factors,
        symbols=symbols,
        partition_strategy=partition_strategy,
        partition_kwargs=partition_kwargs,
        ray_config=ray_config,
        max_concurrent_tasks=max_concurrent_tasks,
        use_actor_pool=use_actor_pool,
        actor_pool_size=actor_pool_size,
        incremental=incremental,
    )
    
    # 测量内存峰值
    import psutil
    process = psutil.Process()
    mem_before = process.memory_info().rss / 1024 / 1024  # MB
    
    start_time = time.perf_counter()
    
    # 运行物化
    result = engine.run()
    
    end_time = time.perf_counter()
    duration = end_time - start_time
    
    # 测量内存峰值
    mem_after = process.memory_info().rss / 1024 / 1024  # MB
    mem_peak = max(mem_before, mem_after)
    
    shutdown_ray()
    
    # 统计结果
    rows_written = result.get("summary", {}).get("rows_written", 0)
    partitions = result.get("summary", {}).get("partitions", 0)
    
    result = BenchmarkResult(
        mode=mode,
        start_date=config["start_date"],
        end_date=config["end_date"],
        num_factors=len(factors),
        num_symbols=len(symbols),
        num_dates=len(get_trading_dates(config["start_date"], config["end_date"])),
        total_work_units=num_factors * len(symbols) * len(get_trading_dates(config["start_date"], config["end_date"])),
        duration_seconds=duration,
        rows_written=rows_written,
        partitions=partitions,
        actor_pool_used=use_actor_pool,
        actor_pool_size=actor_pool_size,
        incremental=incremental,
        memory_peak_mb=mem_peak,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    
    print(f"\nResults:")
    print(f"  Duration: {duration:.1f}s")
    print(f"  Rows written: {rows_written:,}")
    print(f"  Partitions: {partitions}")
    print(f"  Memory peak: {mem_peak:.1f} MB")
    print(f"  Throughput: {result.total_work_units / duration:,.0f} units/s")
    
    return result


def save_results(results: List[BenchmarkResult], output_file: str):
    """保存基准测试结果."""
    data = [asdict(r) for r in results]
    with open(output_file, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Distributed Factorization Benchmark")
    parser.add_argument("--mode", choices=["full", "incremental", "distributed", "single_date", "all"],
                        default="full", help="Benchmark mode")
    parser.add_argument("--output", default="benchmark_results.json", help="Output JSON file")
    parser.add_argument("--actors", type=int, default=8, help="Actor pool size")
    parser.add_argument("--cpus", type=int, default=32, help="CPU cores for Ray")
    args = parser.parse_args()

    # 初始化 Ray
    ray_config = {"mode": "local", "num_cpus": args.cpus}
    init_ray(ray_config)

    try:
        modes = [args.mode] if args.mode != "all" else ["full", "incremental", "distributed", "single_date"]
        results = []
        
        for mode in modes:
            config = get_test_config(mode)
            config["ray_config"] = {"mode": "local", "num_cpus": args.cpus}
            config["use_actor_pool"] = mode == "distributed"
            config["actor_pool_size"] = args.actors if mode == "distributed" else 0
            config["max_concurrent_tasks"] = 0
            
            result = run_benchmark(config, mode)
            results.append(result)
        
        save_results(results, args.output)
        
        # 打印汇总
        print(f"\n{'='*60}")
        print("BENCHMARK SUMMARY")
        print(f"{'='*60}")
        for r in results:
            print(f"  {r.mode:12s} | {r.duration_seconds:6.1f}s | {r.rows_written:>10,} rows | {r.memory_peak_mb:6.1f} MB | {r.duration_seconds/r.total_work_units*1e6:.1f} us/unit")
        
    finally:
        shutdown_ray()


if __name__ == "__main__":
    main()