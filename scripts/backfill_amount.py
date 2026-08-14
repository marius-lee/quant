"""存量 daily.amount 回填入口 — 复用 DataStore.backfill_amount 成熟逻辑.

背景: 2019 年 daily 数据来自早期源, amount 缺失 ~89% (750k 行, 2026-08-14
verify 全量检查发现), 导致物化输入验证失败 (amount<=0 比例 89.5%).
full 模式每 symbol 一次 baostock 查询全区间, 5208 只 ≈ 50 分钟 (含限速),
只 UPDATE amount 缺失行, 口径: baostock 元 → DB 千元 (000070 实测一致).

用法:
    PYTHONPATH=. .venv/bin/python scripts/backfill_amount.py
"""
import sys
import time

from quant.data.store import DataStore


def main() -> int:
    s = DataStore()
    _t0 = time.time()
    n = s.backfill_amount(full=True)
    el = time.time() - _t0
    print(f"amount backfill: {n} rows updated in {el/60:.1f}min", flush=True)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())