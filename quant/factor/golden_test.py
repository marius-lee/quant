"""因子值回归测试 (P3c) — 修改因子函数后自动检测值变化.

使用:
  PYTHONPATH=. .venv/bin/python3 quant/factor/golden_test.py generate  # 生成 golden
  PYTHONPATH=. .venv/bin/python3 quant/factor/golden_test.py verify   # 验证
"""

import json
import os
import sys
import pandas as pd
import numpy as np

_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GOLDEN_PATH = os.path.join(_PROJ, "test", "golden_factors.json")

# 采样日期 (覆盖牛/熊/震荡)
_SAMPLE_DATES = ["2020-03-23", "2021-02-10", "2022-04-26", "2024-09-30", "2025-12-31"]
# 采样股票 (大盘/中盘/小盘)
_SAMPLE_SYMBOLS = ["000001", "000002", "000858", "002415", "300750", "600519",
                   "601318", "688981"]


def generate():
    """生成 golden 数据集。"""
    from quant.data.store import DataStore
    from quant.factor.store import FactorStore
    from quant.factor.compute._dispatch import compute_all_factors

    store = DataStore()
    fs = FactorStore()

    golden = {}
    for date_str in _SAMPLE_DATES:
        print(f"  computing {date_str}...")
        data = store.get_daily(_SAMPLE_SYMBOLS,
                               start=(pd.Timestamp(date_str) - pd.Timedelta(days=400)).strftime("%Y-%m-%d"),
                               end=date_str)
        fundamentals = store.get_fundamentals(_SAMPLE_SYMBOLS, date=date_str)
        fv = compute_all_factors(data, date_str, fundamentals=fundamentals,
                                 factor_fail_fast=False, quiet=True)
        date_results = {}
        for name, series in fv.items():
            if series is not None and series.dropna().count() >= 2:
                date_results[name] = {s: round(float(v), 6) for s, v in series.dropna().items()}
        golden[date_str] = date_results
        print(f"    {len(date_results)} factors")

    store.close()
    os.makedirs(os.path.dirname(_GOLDEN_PATH), exist_ok=True)
    with open(_GOLDEN_PATH, 'w') as f:
        json.dump(golden, f, indent=2)
    print(f"Golden saved: {_GOLDEN_PATH} ({len(golden)} dates)")


def verify(tolerance: float = 1e-4):
    """验证当前因子值与 golden 是否一致。"""
    if not os.path.exists(_GOLDEN_PATH):
        print("ERROR: golden file not found. Run 'generate' first.")
        sys.exit(1)

    with open(_GOLDEN_PATH, 'r') as f:
        golden = json.load(f)

    from quant.data.store import DataStore
    from quant.factor.store import FactorStore
    from quant.factor.compute._dispatch import compute_all_factors

    store = DataStore()
    mismatches = []
    missing_factors = []
    new_factors = []

    for date_str in _SAMPLE_DATES:
        print(f"  verifying {date_str}...")
        data = store.get_daily(_SAMPLE_SYMBOLS,
                               start=(pd.Timestamp(date_str) - pd.Timedelta(days=400)).strftime("%Y-%m-%d"),
                               end=date_str)
        fundamentals = store.get_fundamentals(_SAMPLE_SYMBOLS, date=date_str)
        fv = compute_all_factors(data, date_str, fundamentals=fundamentals,
                                 factor_fail_fast=False, quiet=True)

        expected = golden.get(date_str, {})
        for name in sorted(set(expected.keys()) | set(fv.keys())):
            if name not in expected:
                new_factors.append(f"{date_str}/{name}")
                continue
            if name not in fv or fv[name] is None:
                missing_factors.append(f"{date_str}/{name}")
                continue

            current = fv[name].dropna()
            gold = expected[name]
            for sym, gold_val in gold.items():
                if sym not in current.index:
                    mismatches.append(f"{date_str}/{name}/{sym}: missing")
                    continue
                cur_val = current[sym]
                if abs(cur_val - gold_val) > tolerance:
                    mismatches.append(
                        f"{date_str}/{name}/{sym}: {gold_val:.6f} → {cur_val:.6f}"
                    )

    store.close()

    print(f"\nResults:")
    print(f"  Mismatches:    {len(mismatches)}")
    print(f"  Missing:       {len(missing_factors)}")
    print(f"  New factors:   {len(new_factors)}")

    if mismatches:
        print("\nMISMATCH DETAILS (first 20):")
        for m in mismatches[:20]:
            print(f"  {m}")
        print("\nFAIL: factor values changed! Review factor function modifications.")
        sys.exit(1)
    elif missing_factors:
        print("\nWARN: some factors missing from current run.")
    else:
        print("OK: all factor values match golden.")

    if new_factors:
        print(f"\nNew factors detected ({len(new_factors)}), run 'generate' to update golden.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "generate":
        generate()
    else:
        verify()
