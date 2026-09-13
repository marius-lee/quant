#!/usr/bin/env python
# 用途: 将"真坏因子"(compute_fn 自引用且无 curated 公式, 无法物化/回测) 归档,
#       排除出物化因子缓存与回测策略池 (backtesting U using).
# 版本: v1.0.0
# 用法: PYTHONPATH=. .venv/bin/python scripts/archive_broken_factors.py
# 幂等性: 仅对列出的因子置 status=archived; 已是 archived 者不变.
#
# 背景 (V570 任务1 后续): 7 个因子 compute_fn 自引用且无 curated 公式 →
#   表达式路径已排除其出池, 但注册表状态仍可能停在 evaluating/probation,
#   归档使其状态机一致, 不再参与任何池.
BROKEN = [
    "macro_cpi_yoy", "macro_m2_yoy", "macro_pmi_diff", "macro_rate_10y",
    "seasonality_12m_1m", "tail_risk", "close_surge",
]
REASON = "NO_COMPUTE_FN"


def main():
    from quant.data.repos import FactorRepo
    fr = FactorRepo()
    n = fr.batch_set_status(BROKEN, "archived", reason=REASON)
    print(f"archived {n}/{len(BROKEN)} factors -> status=archived, reason={REASON}")
    for name in BROKEN:
        r = fr.get_factor_by_name(name)
        print(f"  {name}: status={r['status']}, reason={r.get('status_reason')}")


if __name__ == "__main__":
    main()
