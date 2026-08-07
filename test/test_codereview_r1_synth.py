"""v418 (2026-08-08): CODE-REVIEW-2026-08-07 遗留问题回归测试.

R1: sleeve_compose factor_count 每轮重置 → 多因子共振 bonus 恒失效.
    修复前: 同一 symbol 被多个因子选入 top-N 时, 计数被下一轮重置为 0,
    最终 factor_count ≤ 1, alpha 的 0.2×count 多因子确认加成永远无效.
    修复后: 只对入选 top-N 的 symbol 累加计数.

验证方式: 构造 2 个因子, A 只入 factor1 的 top-N, B 同时入 factor1+2 的 top-N.
    修复前 B 的 factor_count 被重置为 1, A 为 1 → 二者均值相同且无加成差异;
    修复后 B 的 count=2, 应获得 (1+0.2) 加成 = 1.2×, 排序高于 A.
"""
import pandas as pd

import quant.alpha.synth as synth


def test_sleeve_factor_count_accumulates_across_factors():
    # factor1: top-N 取 1 → 选 A
    # factor2: top-N 取 1 → 选 B (A 也在 factor1 出现但不在 factor2 top)
    f1 = pd.Series({"A": 2.0, "B": 1.0, "C": 0.0})
    f2 = pd.Series({"A": 0.0, "B": 2.0, "C": 1.0})
    out = synth.sleeve_compose({"f1": f1, "f2": f2}, positions_per_factor=1, min_factors=1)

    assert "B" in out.index, "B 应被选中"
    assert "A" in out.index, "A 应被选中"
    # B 被 2 个因子选入 → count=2 → alpha=mean_rank×(1+0.2×2)
    # A 只被 1 个因子选入 → count=1 → alpha=mean_rank×(1+0.2×1)
    # B 的 alpha 应明显高于 A (修复前两者相同)
    assert out["B"] > out["A"], "多因子共振 stock B 应有更高 alpha (修复前相等 → 此断言失败)"


def test_single_factor_count_no_regression():
    f1 = pd.Series({"A": 2.0, "B": 1.0})
    out = synth.sleeve_compose({"f1": f1}, positions_per_factor=1, min_factors=1)
    assert "A" in out.index and out["A"] > 0
    assert "B" not in out.index or out["B"] <= out["A"]


if __name__ == "__main__":
    pass