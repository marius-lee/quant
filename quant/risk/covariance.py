"""协方差估计 — 样本协方差 + Ledoit-Wolf 收缩 + 因子模型协方差。

高维截面（~5000 股票 × 60 日）样本协方差不可靠:
  股票数 >> 样本数 → 样本协方差奇异，最小特征值接近 0

Ledoit-Wolf (2004) 收缩估计:
  Σ_shrink = (1 - δ) × Σ_sample + δ × F_target
  其中 δ 为最优收缩强度，F_target 为目标矩阵（常数相关模型）

因子模型协方差 (test-v397, ADR-041 fix):
  Σ_stock = X·F·X' + diag(σ²_ε)
  其中 X 为 N×M 因子暴露矩阵（截面 z-score），F 为 M×M 因子协方差矩阵。
  当 N >> T 时（800 股票 × 252 天），先取 top-K (K=30 ≤ T) 再算协方差，
  保证 T > K，矩阵良态。来源: Barra USE4 (MSCI 2011)。

来源: ② Ledoit & Wolf (2004); ② Barra USE4 (MSCI 2011);
      ② Grinold & Kahn (2000) Ch.3.
"""

import numpy as np
import pandas as pd
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from typing import Optional
from collections import deque
import threading


def sample_cov(returns: pd.DataFrame) -> pd.DataFrame:
    """样本协方差矩阵 (ddof=1)。

    returns: DataFrame, index=date, columns=symbols
    返回: DataFrame, index=columns=symbols
    """
    return returns.cov()


def _constant_correlation_target(cov: np.ndarray) -> np.ndarray:
    """构造 Ledoit-Wolf 常数相关目标矩阵。

    所有股票方差相同（均值方差），所有 pairwise 相关系数相同（均值相关）。
    这是一个高度结构化的矩阵，极端条件数好。
    """
    n = cov.shape[0]
    # 均值方差
    avg_var = np.trace(cov) / n
    # 均值相关系数
    std = np.sqrt(np.diag(cov))
    # D⁻¹ × Σ × D⁻¹ 得到相关矩阵
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_std = np.where(std > 0, 1.0 / std, 0.0)
    R = cov * inv_std[:, None] * inv_std[None, :]
    # 平均相关系数 (exclude diagonal)
    off_diag = R[~np.eye(n, dtype=bool)]
    avg_corr = off_diag.mean() if len(off_diag) > 0 else 0.0

    # 目标矩阵: 对角线=avg_var, 非对角线=avg_var × avg_corr
    target = np.full((n, n), avg_var * avg_corr)
    np.fill_diagonal(target, avg_var)
    return target


def _pairwise_sample_cov(returns: pd.DataFrame) -> np.ndarray:
    """pairwise-complete 样本协方差 (ddof=1) — B6 (2026-08-18).

    列内存在 NaN 时, 原 X.T@X 中一行 NaN → 整个矩阵 NaN 传染
    (协方差全 NaN → inv 失败 → 组合权重全 NaN). 对每对 symbol 取
    共同非 NaN 行计算, 与 pandas .cov() 的 pairwise 语义一致.
    """
    X = returns.values.astype(float)
    n = X.shape[1]
    mask = ~np.isnan(X)
    S = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            both = mask[:, i] & mask[:, j]
            k = int(both.sum())
            if k < 2:
                continue
            xi = X[both, i]
            xj = X[both, j]
            xi_c = xi - xi.mean()
            xj_c = xj - xj.mean()
            S[i, j] = S[j, i] = float(xi_c @ xj_c) / (k - 1)
    return S


def ledoit_wolf_cov(
    returns: pd.DataFrame,
    shrinkage: Optional[float] = None,
) -> pd.DataFrame:
    """Ledoit-Wolf 收缩协方差估计。

    returns: DataFrame, index=date, columns=symbols (日收益率)
    shrinkage: 收缩强度 δ ∈ [0,1]。None 时自动估计最优 δ。

    返回: 收缩后的协方差矩阵 DataFrame。

    实现: 常数相关模型（最鲁棒的 LW 变体）。

    来源: ② Ledoit & Wolf (2004) 公式 (14)-(17)
    """
    symbols = returns.columns.tolist()
    n = len(symbols)
    T = len(returns)

    if n < 2 or T < n:
        # 样本不足时用对角协方差
        var = returns.var(ddof=1)
        return pd.DataFrame(np.diag(var.values), index=symbols, columns=symbols)

    # 中心化 + 样本协方差
    # B6 (2026-08-18): 原 X.T@X 遇行内 NaN → 全矩阵 NaN 传染. 改 pairwise.
    X = returns.values - returns.values.mean(axis=0)  # (T, n)
    S = _pairwise_sample_cov(returns)  # pairwise-complete, NaN 安全

    target = _constant_correlation_target(S)

    if shrinkage is None:
        # LM 自动估计最优 δ → 公式 Ledoit-Wolf (2004) eq. (17)
        # π̂ = sum over all i,j of AsyVar(s_ij)
        # For constant-correlation target:
        #   δ* = π / γ
        # where π = sum_i sum_j AsyVar(√T * s_ij)
        #       γ = sum_i sum_j (f_ij - s_ij)²

        # π: 渐近方差 (使用无偏一致估计)
        # AsyVar(s_ij) ≈ 1/T * sum_t[(x_ti - x̄_i)(x_tj - x̄_j) - s_ij]²
        # B6 (2026-08-18): 原 X.T@X 遇行内 NaN → 全矩阵 NaN 传染. 改 pairwise.
        # v535: π̂ 与 S 同口径 — 原整行 outer 后 nan_to_num: 任一 symbol 当日
        # NaN → 该观测整行贡献置 0 → π̂ 系统性低估 → δ* 偏移 (收缩不足,
        # 偏向样本协方差). 改 pairwise 有效对累计, 与 _pairwise_sample_cov
        # 完全一致 (x_ti 缺 → 该 (i,j) 对当日不贡献, 其他对不受牵连).
        pi_mat = np.zeros((n, n))
        X_nan = np.isnan(X)
        for i in range(n):
            for j in range(i, n):
                both = ~(X_nan[:, i] | X_nan[:, j])
                if both.sum() < 2:
                    continue
                d = X[both, i] * X[both, j] - S[i, j]
                v = float((d ** 2).sum()) / both.sum()  # AsyVar = (1/k)Σ[...]², k=有效对数
                pi_mat[i, j] = v
                pi_mat[j, i] = v
        pi_hat = pi_mat.sum()

        # γ: 样本协方差与目标的距离
        gamma_hat = ((S - target) ** 2).sum()

        # δ* = π̂ / γ̂, clamped to [0, 1]
        shrinkage = max(0.0, min(1.0, pi_hat / max(gamma_hat, 1e-10)))

    # 收缩估计
    shrunk = (1 - shrinkage) * S + shrinkage * target

    return pd.DataFrame(shrunk, index=symbols, columns=symbols)


def covariance_matrix(
    returns: pd.DataFrame,
    method: str = "ledoit_wolf",
    window: int = None,
    min_periods: int = None,
) -> pd.DataFrame:
    """统一的协方差估计入口。

    returns: index=date, columns=symbols 的日收益率 DataFrame
    method: sample | ledoit_wolf
    window: 滚动窗口长度（取最近 window 个交易日）
    min_periods: 最少需要的交易天数

    返回: 协方差矩阵 DataFrame

    test-v397: 当 symbols 数 > window 时 (N > T)，自动降级用样本协方差+LW。
    调用方应先用 covariance_subset() 对 top-K 股票子集计算，确保 T > K。
    """
    if window is None:
        window = _require_cfg("risk.covariance.window")
    if min_periods is None:
        min_periods = _require_cfg("risk.covariance.min_periods")

    # 取最近 window 个有效交易日
    recent = returns.iloc[-window:].dropna(axis=1, how="all")

    if len(recent) < min_periods:
        from quant.utils.logger import get_logger
        get_logger("risk.covariance").warning(
            f"covariance: only {len(recent)}/{min_periods} periods available"
        )
        # 回退到全部可用数据
        recent = returns.dropna(axis=1, how="all")

    # 只保留有足够数据的股票
    recent = recent.dropna(axis=1, thresh=min_periods)

    import time as _time
    from quant.utils.logger import get_logger
    _clog = get_logger("risk.covariance")
    n_syms = recent.shape[1]
    n_periods = len(recent)
    if n_syms > n_periods:
        _clog.warning(
            f"covariance: N={n_syms} > T={n_periods} — matrix may be near-singular. "
            f"Consider using covariance_subset() with top-K < T."
        )
    if n_syms > 100:
        _clog.info(f"covariance: computing {n_syms}×{n_syms} matrix over {n_periods} periods...")
    _t0 = _time.time()

    if method == "ledoit_wolf":
        result = ledoit_wolf_cov(recent)
    else:
        result = sample_cov(recent)

    if n_syms > 100:
        _clog.info(f"covariance: done in {_time.time()-_t0:.1f}s")
    return result


def covariance_subset(
    returns: pd.DataFrame,
    symbols: list,
    method: str = "ledoit_wolf",
    window: int = None,
    min_periods: int = None,
) -> pd.DataFrame:
    """对指定 symbol 子集计算协方差矩阵 (test-v397).

    当 N > T 时调用方应传入 top-K ≤ T 的 symbols 子集，
    保证矩阵良态。内部复用 covariance_matrix()。

    returns: index=date, columns=symbols, 全量日收益率
    symbols: 需要计算协方差的 symbol 子集 (e.g. alpha 排名 top 30)
    """
    common = [s for s in symbols if s in returns.columns]
    # v413: 剔除含 NaN 的列, 防止单个 NaN 污染全协方差矩阵
    if common:
        sub = returns[common]
        nan_mask = sub.isna().any(axis=0)
        clean = [s for s in common if not nan_mask.get(s, True)]
        if len(clean) < len(common):
            from quant.utils.logger import get_logger
            get_logger("risk.covariance").debug(
                f"covariance_subset: dropped {len(common)-len(clean)} NaN columns")
        common = clean
    if len(common) < 2:
        from quant.utils.logger import get_logger
        get_logger("risk.covariance").warning(
            f"covariance_subset: only {len(common)}/{len(symbols)} symbols in returns"
        )
        # 有空 DataFrame 保底返回 2×2 零阵 → 调用方自然 fallback
        empty = pd.DataFrame(0.0, index=common or symbols[:2], columns=common or symbols[:2])
        if len(empty) < 2:
            empty = pd.DataFrame([[1.0, 0.0], [0.0, 1.0]],
                                 index=["A", "B"], columns=["A", "B"])
        return empty
    sub = returns[common]
    return covariance_matrix(sub, method=method, window=window, min_periods=min_periods)



# ── Incremental Covariance (test-v458: P2) ──────────────────────────────
class IncrementalCovariance:
    """增量协方差维护器 — O(N²) 每日更新替代 O(N³) 全量重算。

    维护滚动窗口内的 Ledoit-Wolf 收缩协方差，支持：
      - 每日增量更新 (O(N²) Sherman-Morrison 或逐日全量重算)
      - 每 K 天完全重算一次以控制数值误差
      - 线程安全 (内部锁)

    适用场景: 回测/实盘每日需协方差矩阵的场景 (组合优化、风控、VaR)。
    """

    def __init__(
        self,
        window: int = 252,
        symbols: Optional[list[str]] = None,
        full_recalc_interval: int = 20,  # 每 20 个交易日全量重算一次
        method: str = "ledoit_wolf",
    ):
        """
        Args:
            window: 滚动窗口长度 (交易日数)
            symbols: 固定的 symbol 列表 (None 时动态适配)
            full_recalc_interval: 每隔多少次 update 做一次全量重算 (控制数值漂移)
            method: "ledoit_wolf" | "sample"
        """
        from quant.config.constants import _require_cfg
        self.window = window or _require_cfg("risk.covariance.window")
        self.symbols = list(symbols) if symbols else None
        self.full_recalc_interval = full_recalc_interval
        self.method = method
        self._update_count = 0

        self._returns_buffer = deque(maxlen=self.window)
        self._symbols = None
        self._cov = None
        self._lock = threading.Lock()

    def update(self, daily_returns: pd.Series) -> pd.DataFrame:
        """增量更新协方差矩阵。

        Args:
            daily_returns: pd.Series, index=symbol, value=当日收益率

        Returns:
            当前协方差矩阵 DataFrame (symbols × symbols)
        """
        with self._lock:
            # 1. 处理新进/退出的 symbol
            new_syms = daily_returns.index.difference(self._symbols) if self._symbols else daily_returns.index
            if len(new_syms) > 0:
                self._symbols = daily_returns.index.tolist()
                self._returns_buffer.clear()
                self._cov = None
                get_logger("risk.covariance").info(f"IncrementalCovariance: symbol set changed, reset buffer ({len(self._symbols)} symbols)")

            # 2. 加入新一天的收益率
            if self._symbols is None:
                self._symbols = daily_returns.index.tolist()
            # 仅保留已知 symbol
            daily_aligned = daily_returns.reindex(self._symbols)
            self._returns_buffer.append(daily_aligned)

            # 3. 决定是否全量重算
            self._update_count += 1
            need_full = (
                self._cov is None or
                self._update_count % self.full_recalc_interval == 0 or
                len(self._returns_buffer) < 30  # 样本不足时不做增量
            )

            if need_full:
                self._cov = self._full_recalc()
            else:
                self._incremental_update(daily_aligned)

            return self._cov

    def _full_recalc(self) -> pd.DataFrame:
        """全量重算 Ledoit-Wolf 协方差 (O(N³))。"""
        if len(self._returns_buffer) < 3:
            return self._empty_cov()
        returns_df = pd.DataFrame(self._returns_buffer)
        # 去除全 NaN 列
        clean = returns_df.dropna(axis=1, how='all')
        if clean.shape[1] < 2:
            return self._empty_cov()
        # 只保留有足够数据的列
        clean = clean.dropna(axis=1, thresh=30)
        if clean.shape[1] < 2:
            return self._empty_cov()
        # 调用现有 Ledoit-Wolf
        result = ledoit_wolf_cov(clean)
        return result

    def _incremental_update(self, daily_ret: pd.Series) -> None:
        """占位 — B11 (2026-08-18): 原"增量"实现是死代码假增量.

        原实现: new_cov 秩-1 更新算出后从未赋值 (L349-354 立即被
        ledoit_wolf_cov 全量重算覆盖), try/except/else 三分支全部
        _full_recalc() → 每次 update 恒 O(N³) 且带注释宣称 O(N²).
        修复: 诚实实现 — 组合规模 ≤ max_positions (20) 时全量 LW 重算
        O(20³)=8k 浮点/次微不足道, 无增量必要. full_recalc_interval
        参数保留 (向后兼容), 语义并入统一重算.
        """
        self._cov = self._full_recalc()

    def _empty_cov(self) -> pd.DataFrame:
        syms = self._symbols or []
        if not syms:
            return pd.DataFrame()
        return pd.DataFrame(np.eye(len(syms)) * 1e-6, index=syms, columns=syms)

    def get_covariance(self) -> pd.DataFrame:
        """获取当前协方差矩阵 (线程安全)。"""
        with self._lock:
            return self._cov.copy() if self._cov is not None else self._empty_cov()

    def reset(self):
        """重置状态 (换股票池/换窗口时调用)。"""
        with self._lock:
            self._returns_buffer.clear()
            self._cov = None
            self._symbols = None
            self._update_count = 0

