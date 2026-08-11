#!/usr/bin/env python3
"""
CSV → Parquet 迁移脚本
将 factor_cache/ 下的 *.csv.gz 全量迁移到 parquet/date=YYYY-MM-DD/factor_name.parquet
使用方式:
    PYTHONPATH=. python migrate_csv_to_parquet.py [--dry-run]
"""

import os
import sys
import gzip
import argparse
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quant.factor.store import FactorStore

def migrate_one_file(csv_path, parquet_dir, dry_run=False):
    """迁移单个 CSV.gz 文件到 Parquet 分区"""
    filename = os.path.basename(csv_path)
    date_str = filename.replace(".csv.gz", "")
    if not date_str or len(date_str) != 10:
        return f"SKIP {filename}: invalid date format"

    pdir = os.path.join(parquet_dir, f"date={date_str}")
    if not dry_run:
        os.makedirs(pdir, exist_ok=True)

    # 读取 CSV.gz (格式: symbol,factor,value,date)
    records = []
    with gzip.open(csv_path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",", 3)  # 最多分 4 列
            if len(parts) < 3:
                continue
            sym, fname, val_str = parts[0], parts[1], parts[2]
            try:
                val = float(val_str)
            except ValueError:
                continue
            records.append((sym, fname, val, date_str))

    if not records:
        return f"SKIP {filename}: no valid rows"

    df = pd.DataFrame(records, columns=["symbol", "factor", "value", "date"])
    df["value"] = df["value"].astype("float32")
    df = df.drop_duplicates(subset=["symbol", "factor", "date"], keep="last")

    if dry_run:
        factors = df["factor"].nunique()
        rows = len(df)
        return f"DRY-RUN {filename}: {rows} rows, {factors} factors"

    # 按因子分组写入
    for fname, fdf in df.groupby("factor"):
        pdir_factor = os.path.join(parquet_dir, f"date={date_str}")
        os.makedirs(pdir_factor, exist_ok=True)
        ppath = os.path.join(pdir_factor, f"{fname}.parquet")
        fdf.to_parquet(ppath, compression="zstd", compression_level=3, index=False)

    return f"OK {filename}: {len(df)} rows, {df['factor'].nunique()} factors"

def main():
    parser = argparse.ArgumentParser(description="Migrate factor CSV cache to Parquet")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be done")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers")
    parser.add_argument("--cache-dir", default=None, help="Custom factor_cache directory")
    args = parser.parse_args()

    # 默认缓存目录
    if args.cache_dir:
        cache_dir = args.cache_dir
    else:
        # 修正：使用项目根目录
        cache_dir = "/Users/mariusto/project/quant/quant/data/factor_cache"

    parquet_dir = os.path.join(cache_dir, "parquet")
    os.makedirs(parquet_dir, exist_ok=True)

    # 扫描所有 CSV.gz
    csv_files = [f for f in os.listdir(cache_dir) if f.endswith(".csv.gz")]
    csv_files.sort()

    if not csv_files:
        print("No CSV files found to migrate.")
        return

    print(f"Found {len(csv_files)} CSV files to migrate")
    print(f"Target: {parquet_dir}")
    if args.dry_run:
        print("*** DRY RUN MODE ***")

    # 并行迁移
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(migrate_one_file, os.path.join(cache_dir, f), parquet_dir, args.dry_run): f for f in csv_files}
        for i, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            results.append(result)
            if i % 20 == 0 or i == len(csv_files):
                print("  Progress: {}/{} - {}".format(i, len(csv_files), result))

    # 统计
    ok = sum(1 for r in results if r.startswith("OK"))
    skipped = sum(1 for r in results if r.startswith("SKIP"))
    dry = sum(1 for r in results if r.startswith("DRY-RUN"))
    print("=== Migration Summary ===")
    print("Total files: {}".format(len(csv_files)))
    print("Migrated: {}".format(ok))
    print("Skipped: {}".format(skipped))
    if args.dry_run:
        print("Dry-run: {}".format(dry))

if __name__ == "__main__":
    main()
