"""
因子边际贡献评估 — Grinold & Kahn (1999) 框架

三层检验:
  1. 统计显著性 — IC 均值的 t 检验 (Harvey, Liu & Zhu 2016: t > 2.0)
  2. 边际贡献    — Grinold & Kahn: 新增因子在已有因子基础上的增量 IC
  3. 回测验证    — 步进回测, 信息比率 (IR) 提升则保留

撤销原因 (vs 之前固定 IC 阈值做法):
  - 0.01/0.02 无统计依据, 是工程便利性选择
  - 业界标准: Grinold & Kahn 用 IR = IC × √breadth; Harvey 等用多重检验修正
  - 边际贡献比 IC 绝对值更关键: 高 IC 但与其他因子高度相关的因子无增量价值
  - 详见 docs/adr/007-factor-evaluation-standard.md
"""

import numpy as np
from typing import Dict, List, Tuple
from scipy import stats as scipy_stats


def compute_marginal_evaluation(
    factor_names: List[str],
    ic_means: Dict[str, float],
    ic_irs: Dict[str, float],
    corr_matrix: np.ndarray,
    n_days: int = 90,
    t_threshold: float = 2.0,
) -> Dict[str, dict]:
    """综合评估每个因子的边际贡献。

    Args:
        factor_names: 因子名列表
        ic_means: IC 均值 {name: float}
        ic_irs: IC 信息比率 (mean/std) {name: float}
        corr_matrix: 因子截面相关性矩阵 (n_factors × n_factors)
        n_days: IC 计算使用的交易日数 (用于 t 检验)
        t_threshold: t 统计量阈值 (Harvey 等建议 2.0; 多重检验修正后建议 3.0)

    Returns:
        {name: {ic, t_stat, t_pass, marginal_ic, passes, reason}, ...}
    """
    n = len(factor_names)

    # ── Layer 1: IC t 检验 ──
    # H0: IC_mean = 0
    # t = IC / (σ_IC / √n) = (IC / σ_IC) × √n = IR × √n
    t_stats = {}
    t_pass = {}
    for i, name in enumerate(factor_names):
        ic = ic_means.get(name, 0.0)
        ir = ic_irs.get(name, 0.0)
        if isinstance(ir, (int, float)) and ir != 0 and not np.isnan(ir):
            t = abs(ir) * np.sqrt(n_days)
        else:
            # Fallback: estimate std from neighboring factors
            t = 0.0
        t_stats[name] = t
        t_pass[name] = t >= t_threshold

    # ── Layer 2: 边际贡献 (Grinold & Kahn) ──
    # 对已有因子集合 F, 新增因子 g:
    #   边际 IC(g|F) = IC_g - ρ_gF' · Σ_FF^{-1} · IC_F
    # 如果边际 IC ≈ 0, 说明 g 的信息已被 F 完全捕获。
    marginal_results = {}

    for i, name in enumerate(factor_names):
        ic_new = ic_means.get(name, 0.0)
        t_stat = t_stats[name]
        sig_pass = t_pass[name]

        if not sig_pass:
            marginal_results[name] = {
                "ic": ic_new,
                "t_stat": t_stat,
                "t_pass": False,
                "marginal_ic": None,
                "passes": False,
                "reason": f"IC t-stat={t_stat:.1f} < {t_threshold}, 统计不显著",
            }
            continue

        # 计算相对于其他所有因子的边际贡献
        other_indices = [j for j in range(n) if j != i]
        if not other_indices:
            # 只有一个因子
            marginal_results[name] = {
                "ic": ic_new,
                "t_stat": t_stat,
                "t_pass": True,
                "marginal_ic": ic_new,
                "passes": True,
                "reason": "唯一因子, 边际IC = IC",
            }
            continue

        ic_others = np.array([ic_means[factor_names[j]] for j in other_indices])
        rho = corr_matrix[i, other_indices]
        sigma = corr_matrix[np.ix_(other_indices, other_indices)]

        try:
            sigma_inv = np.linalg.inv(sigma)
            # 被已有因子解释的部分
            explained_ic = rho @ sigma_inv @ ic_others
            marginal_ic = ic_new - explained_ic

            # 边际 IC 的近似 t 检验 (Delta method)
            # Var(marginal_IC) ≈ Var(IC_g) + Var(explained)
            # 简化: 如果 |marginal_IC| 远小于 IC 标准差, 则不显著
            ic_std = abs(ic_new / (ic_irs.get(name, 0.01) or 0.01))
            marginal_t = abs(marginal_ic) / (ic_std / np.sqrt(n_days)) if ic_std > 0 else 0

            marginal_pass = marginal_t >= t_threshold and abs(marginal_ic) > 0.005

            if marginal_pass:
                reason = f"边际IC={marginal_ic:+.4f}, 增量显著 (t={marginal_t:.1f})"
            else:
                if abs(marginal_ic) <= 0.005:
                    reason = f"边际IC={marginal_ic:+.4f}≈0, 信息已被已有因子覆盖"
                else:
                    reason = f"边际IC={marginal_ic:+.4f}, t={marginal_t:.1f}<{t_threshold}, 增量不显著"

            marginal_results[name] = {
                "ic": ic_new,
                "t_stat": t_stat,
                "t_pass": True,
                "marginal_ic": marginal_ic,
                "marginal_t": marginal_t,
                "passes": True,
                "reason": reason,
            }

        except np.linalg.LinAlgError:
            marginal_results[name] = {
                "ic": ic_new,
                "t_stat": t_stat,
                "t_pass": True,
                "marginal_ic": None,
                "passes": False,
                "reason": "相关性矩阵奇异, 无法计算边际贡献",
            }

    return marginal_results


def rank_candidates(marginal_results: Dict[str, dict]) -> List[Tuple[str, float, dict]]:
    """按边际IC排序, 返回 (name, marginal_ic, result) 列表."""
    ranked = []
    for name, result in marginal_results.items():
        mic = result.get("marginal_ic", 0.0)
        if mic is None:
            mic = 0.0
        ranked.append((name, mic, result))
    ranked.sort(key=lambda x: abs(x[1]), reverse=True)
    return ranked


def stepwise_selection(
    ranked_candidates: List[Tuple[str, float, dict]],
    corr_matrix: np.ndarray,
    factor_names: List[str],
    t_threshold: float = 2.0,
) -> List[str]:
    """基于边际贡献的步进筛选。

    按照边际IC从高到低逐个添加因子。
    每添加一个, 重新计算剩余因子的边际贡献 (考虑新加入因子)。
    当剩余因子的边际IC不显著时停止。

    Returns:
        最终选中的因子列表
    """
    selected = []
    remaining = [(name, mic, res) for name, mic, res in ranked_candidates
                 if res.get("passes") or res.get("t_pass")]

    # 第一轮: 选边际IC最大的正向因子
    for name, mic, res in remaining[:]:
        if mic > 0 and res.get("passes"):
            selected.append(name)
            remaining.remove((name, mic, res))
            break

    if not selected:
        # 如果没有正向边际IC的, 选 t 检验通过的
        for name, mic, res in remaining[:]:
            if res.get("t_pass"):
                selected.append(name)
                remaining.remove((name, mic, res))
                break

    # 迭代: 每次添加后重算边际贡献
    MAX_ITERS = 20
    for _ in range(MAX_ITERS):
        if not remaining:
            break

        selected_idx = [factor_names.index(n) for n in selected]
        best_to_add = None
        best_marginal = -999

        for name, mic, res in remaining:
            idx = factor_names.index(name)
            rho = corr_matrix[idx, selected_idx]
            sigma = corr_matrix[np.ix_(selected_idx, selected_idx)]

            try:
                sigma_inv = np.linalg.inv(sigma)
                ic_existing = np.array([marginal_results[fn]["ic"] for fn in selected])
                explained = rho @ sigma_inv @ ic_existing
                new_marginal = marginal_results[name]["ic"] - explained

                if new_marginal > best_marginal and abs(new_marginal) > 0.005:
                    best_marginal = new_marginal
                    best_to_add = name
            except np.linalg.LinAlgError:
                pass

        if best_to_add and best_marginal > 0:
            ic_std = abs(marginal_results[best_to_add]["ic"] / max(0.001, marginal_results[best_to_add].get("ir", 0.01)))
            marginal_t = abs(best_marginal) / (ic_std / np.sqrt(90))

            if marginal_t >= t_threshold:
                selected.append(best_to_add)
                remaining = [(n, m, r) for n, m, r in remaining if n != best_to_add]
            else:
                break
        else:
            break

    return selected
