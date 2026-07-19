#!/usr/bin/env python3
"""同步沪深300指数日线数据到 benchmark_daily 表"""
from quant.utils.excepthook import setup; setup()
from quant.data.benchmark import sync_benchmark

n = sync_benchmark("000300")
print(f"Done: {n} new rows")
if n == 0:
    print("(no new data — already up to date)")
