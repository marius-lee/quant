"""Alpha 模型 — Layer 3: 因子合成 → 收益预测 → 截面排名。

职责: 将多个因子合成为单一 alpha 向量（预期收益），然后做横截面排名。
Alpha 层回答: 「在当前截面上，哪些股票最值得持有？」

来源: ② Grinold & Kahn (2000) Chapter 8 — Alpha 合成与截面排名
"""

import numpy as np
import pandas as pd
from typing import Optional

from factor.base import FactorStats
from factor.compute import compute_all_factors, FACTOR_REGISTRY
from factor.synth import synthesize


class AlphaModel:
    """Alpha 模型: 因子合成 + 截面排名。
    
    工作流程:
      1. calibrate(history) → 用历史 IC 校准因子权重
      2. predict(date, data) → 计算各因子值 → 合成 alpha → 截面排名
      3. cross_sectional_rank(alpha) → 分位数标准化 [0, 1]
    
    配置:
      method: equal_weight | ic_weighted  — 因子合成方式
      train_window: 252                    — IC 校准窗口 (交易日)
      retrain_freq: 20                     — 重校准频率 (交易日)
      top_fraction: 0.30                   — 选股池保留比例
    """
    
    def __init__(
        self,
        method: str = "ic_weighted",
        train_window: int = 252,
        retrain_freq: int = 20,
        top_fraction: float = 0.30,
    ):
        self.method = method
        self.train_window = train_window
        self.retrain_freq = retrain_freq
        self.top_fraction = top_fraction
        
        self._ic_weights: dict = {}        # {factor_name: |IC|}
        self._last_calibrate_date: Optional[str] = None
        self._n_calls = 0                  # 调用计数，判断是否需要重校准
    
    def calibrate(
        self,
        factor_stats: dict[str, FactorStats],
    ) -> dict:
        """用因子评估结果校准权重。
        
        factor_stats: {factor_name: FactorStats} — 从 factor_report() 获取
        
        返回: {factor_name: weight} 归一化权重 dict。
        权重 = |rank_ic_mean| / sum(|rank_ic_mean|)，仅使用 IC 显著的因子。
        
        来源: ② Grinold & Kahn (2000) — IC 加权
        """
        from config.loader import get as cfg
        min_abs_ic = cfg("factor.min_abs_ic", 0.02)
        
        weights = {}
        for name, stats in factor_stats.items():
            if abs(stats.rank_ic_mean) >= min_abs_ic and stats.n_periods >= 10:
                weights[name] = abs(stats.rank_ic_mean)
        
        if not weights:
            # 无显著因子时, 等权使用动量类因子 (保守回退)
            for name in factor_stats:
                if "momentum" in name:
                    weights[name] = 0.02  # 最小 IC 阈值
            if not weights:
                weights = {n: 1.0 for n in factor_stats}
        
        # 归一化
        total = sum(weights.values())
        self._ic_weights = {k: v / total for k, v in weights.items()}
        
        from utils.logger import get_logger
        logger = get_logger("alpha.model")
        logger.info(
            f"calibrated: {len(self._ic_weights)}/{len(factor_stats)} factors, "
            f"top3: {sorted(self._ic_weights.items(), key=lambda x: -x[1])[:3]}"
        )
        return dict(self._ic_weights)
    
    def predict(
        self,
        data: pd.DataFrame,
        date: str,
        ic_scores: Optional[dict] = None,
        fundamentals: Optional[pd.DataFrame] = None,
    ) -> pd.Series:
        """在指定日期截面上计算 alpha 得分。
        
        data: 由 DataStore.get_daily() 返回的宽表 DataFrame
        date: 预测日期 (YYYY-MM-DD)
        ic_scores: {factor_name: IC_mean} — 若提供则用于 IC 加权合成
        fundamentals: 基本面数据 DataFrame (pe, pb, total_mv, industry)
        
        返回: Series(index=symbol, value=alpha_score)。高分 = 值得买。
        """
        self._n_calls += 1
        
        # 1. 计算所有因子值 (价格因子 + 基本面因子)
        factor_values = compute_all_factors(data, date, fundamentals=fundamentals)
        
        # 2. 合并为 DataFrame
        df = pd.DataFrame(factor_values).dropna(how="all")
        if df.empty:
            return pd.Series(dtype=float, name="alpha")
        
        # 3. 合成 alpha
        if self.method == "ic_weighted" and ic_scores:
            alpha = synthesize(
                {k: v for k, v in factor_values.items() if k in ic_scores},
                ic_scores,
                method="ic_weighted",
            )
        elif self._ic_weights:
            # 使用已校准的权重 → 加权平均
            alpha = pd.Series(0.0, index=df.index)
            for name, w in self._ic_weights.items():
                if name in df.columns:
                    alpha += df[name].fillna(0) * w
            # z-score 标准化
            alpha = (alpha - alpha.mean()) / alpha.std(ddof=1)
        else:
            alpha = synthesize(factor_values, method="equal_weight")
        
        alpha.name = "alpha"
        return alpha
    
    def cross_sectional_rank(self, alpha: pd.Series) -> pd.Series:
        """截面分位数标准化 → [0, 1]。
        
        alpha: index=symbol, value=score (高分=值得买)
        返回: index=symbol, value=分位数 (1.0=最优, 0.0=最差)
        
        来源: ② Grinold & Kahn (2000) — 截面排名消除因子分布尾部影响
        """
        if alpha.dropna().empty:
            return alpha
        return alpha.rank(pct=True)
    
    def select_candidates(
        self,
        alpha: pd.Series,
        top_fraction: Optional[float] = None,
    ) -> pd.Series:
        """选股: 保留 Top N% 的候选池。
        
        alpha: 原始 alpha 得分 (非排名)
        top_fraction: 保留比例, 默认 self.top_fraction
        
        返回: 满足条件的 alpha Series (已过滤)
        """
        if top_fraction is None:
            top_fraction = self.top_fraction
        
        ranks = self.cross_sectional_rank(alpha)
        threshold = 1.0 - top_fraction
        candidates = alpha[ranks >= threshold].dropna()
        
        from utils.logger import get_logger
        logger = get_logger("alpha.model")
        logger.info(
            f"candidate pool: {len(candidates)}/{len(alpha.dropna())} "
            f"(top {top_fraction*100:.0f}%)"
        )
        return candidates.sort_values(ascending=False)
    
    def get_top_n(
        self,
        alpha: pd.Series,
        n: int = 20,
        stock_names: Optional[dict] = None,
    ) -> list[dict]:
        """返回 Top N 候选股票列表（含名称和得分）。
        
        返回: [{symbol, name, score, rank_pct}, ...]
        """
        ranks = self.cross_sectional_rank(alpha)
        top = alpha.nlargest(n)
        
        result = []
        for sym in top.index:
            item = {
                "symbol": sym,
                "score": round(top[sym], 4),
                "rank_pct": round(ranks[sym], 4),
            }
            if stock_names and sym in stock_names:
                item["name"] = stock_names[sym]
            result.append(item)
        return result
    
    @classmethod
    def from_config(cls) -> "AlphaModel":
        """从 config.yaml 创建 AlphaModel 实例。"""
        from config.loader import get as cfg
        return cls(
            method=cfg("alpha.method", "ic_weighted"),
            train_window=cfg("alpha.train_window", 252),
            retrain_freq=cfg("alpha.retrain_freq", 20),
            top_fraction=cfg("alpha.top_fraction", 0.30),
        )
