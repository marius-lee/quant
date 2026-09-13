#!/usr/bin/env python
# 用途: 修复因子注册表中 compute_fn 为"自引用占位/空"的表达式因子 —
#       将其写回 factor_curator._CURATED_FACTORS 中的真实公式, 使物化/回测能编译计算.
# 版本: v1.0.0
# 用法: PYTHONPATH=. .venv/bin/python scripts/fix_expr_factor_compute_fn.py
# 幂等性: 仅当 compute_fn 为 None/空/自引用 且 在 _CURATED_FACTORS 有公式时才更新; 已正确者跳过.
#
# 背景 (V569 任务1): curator 注册 7 个表达式因子, 其中 5 个 compute_fn 被写成
# 因子自身名字 (自引用占位) 而非公式 → 物化编译成 data['xxx'] 列不存在 → 全 NaN.
# 修复后 compute_all_factors 按 compute_fn 编译公式, 因子方可物化.
import logging
from quant.data.repos import FactorRepo
from quant.factor.factor_curator import _CURATED_FACTORS

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("fix_expr")

CURATED = {f["name"]: f["expression"] for f in _CURATED_FACTORS}
ALL_STATUS = ("evaluating", "active", "probation", "archived", "draft", "rejected")


def main():
    fr = FactorRepo()
    names = [r["name"] for r in fr.get_all_by_status(ALL_STATUS)]
    fixed, skipped_ok, broken_no_formula = [], [], []
    for name in names:
        cf = fr.get_compute_fn(name)
        is_broken = cf is None or cf.strip() == "" or cf.strip() == name
        if not is_broken:
            continue
        if name in CURATED:
            fr.update_compute_fn(name, CURATED[name])
            fixed.append(name)
            _log.info(f"FIXED  {name}: compute_fn -> {CURATED[name]!r}")
        else:
            broken_no_formula.append(name)
            _log.warning(f"NOFORMULA {name}: compute_fn={cf!r} 且无 curated 公式, 跳过 (建议归档)")
    _log.info(f"\n总计: fixed={len(fixed)} skipped_ok(已有公式)={len(skipped_ok)} "
              f"broken_no_formula={len(broken_no_formula)}")
    if broken_no_formula:
        _log.warning(f"需人工决策归档: {broken_no_formula}")


if __name__ == "__main__":
    main()
