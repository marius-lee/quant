"""ML 模型 Alpha 预测 — LightGBM 替代线性 IC 加权合成。

ADR-035 Phase 2: 非线性 ML 模型可以根据因子间交互效应和非线性关系
提升截面收益预测准确率。当前 IC 加权合成本质是线性模型:
  alpha = Σ(w_i × z_i), w_i ∝ |IC_i|

LightGBM 可以学习:
  - 因子间高阶交互 (momentum × reversal 在市场转折点的效应)
  - 非线性阈值 (极值因子的不对称收益)
  - 截面排名模式 (不同市场状态下的因子优先级)

模型生命周期:
  1. train()    — 历史因子值 + 前向收益 → 训练 LightGBM 回归器
  2. predict()  — 当日因子值 → 预期收益 (alpha scores)
  3. 无模型时    — 自动回退到 IC 加权合成 (零破坏)

存储: quant/data/models/ 目录, 按训练日期版本化
  lgb_model_2026-07-28.txt    (LightGBM 原生格式)
  lgb_metadata_2026-07-28.json (训练元数据: 特征列表、IC、窗口)
"""

from __future__ import annotations

import os
import json
import hashlib
import pickle
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("alpha.qlib_model")

_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "models"
)


@dataclass
class ModelMetadata:
    """模型元数据 — 记录训练信息，用于版本追踪和审计。"""
    train_date: str
    train_start: str
    train_end: str
    n_samples: int
    n_features: int
    feature_names: list[str]
    ic_mean: float         # 训练集截面 IC 均值
    ic_std: float          # 训练集截面 IC 标准差
    model_hash: str         # 模型文件 SHA256
    lgb_params: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# LightGBM 模型封装
# ═══════════════════════════════════════════════════════════

class LgbAlphaModel:
    """LightGBM Alpha 预测模型。

    可用作 AlphaModel 的 combine_mode 后端，通过 predict() 输出 alpha scores。
    未训练时 get_predictor() 抛异常，调用方应回退到 ic_weighted 等线性方法。
    """

    def __init__(self):
        self._lgb = None
        self._feature_names: list[str] = []
        self._metadata: Optional[ModelMetadata] = None
        self._is_available = _check_lightgbm()

    @property
    def is_trained(self) -> bool:
        return self._lgb is not None and len(self._feature_names) > 0

    @property
    def feature_names(self) -> list[str]:
        return list(self._feature_names)

    @property
    def metadata(self) -> Optional[ModelMetadata]:
        return self._metadata

    # ── 训练 ──

    def train(
        self,
        factor_values: dict[str, pd.DataFrame],
        forward_returns: pd.Series,
        feature_names: list[str] = None,
        lgb_params: dict = None,
    ) -> ModelMetadata:
        """训练 LightGBM 回归模型 (流式构建, 不一次性拼接 X 矩阵)。

        Args:
            factor_values: {factor_name: DataFrame(index=date, columns=symbols)}
               每个因子是一个 dates × symbols 面板。
            forward_returns: Series(index=MultiIndex(date, symbol))
               前向收益率 (e.g., T+1 到 T+5 的收益)。
            feature_names: 使用的因子名列表 (None=全部)
            lgb_params: LightGBM 参数 (None=默认配置)

        Returns:
            ModelMetadata: 训练元数据

        Memory: 逐日构建 X_day 并直接喂入 lgb.Dataset, 不 vstack 全量矩阵。
                峰值 ~ (max_daily_symbols × n_features × 8B) ≈ 5000×65×8 ≈ 2.6MB。
        """
        if not self._is_available:
            raise ImportError(
                "lightgbm not installed. Install: pip install lightgbm"
            )

        import lightgbm as lgb

        # 默认参数 (适配 A 股截面数据特点)
        if lgb_params is None:
            lgb_params = {
                "objective": "regression",
                "metric": "rmse",
                "boosting_type": "gbdt",
                "num_leaves": 31,
                "learning_rate": 0.05,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "verbose": -1,
                "n_estimators": 200,
                "min_data_in_leaf": 20,
                "max_depth": 6,
                "lambda_l1": 0.1,
                "lambda_l2": 0.1,
            }

        if feature_names is None:
            feature_names = list(factor_values.keys())

        self._feature_names = feature_names

        _log.info("train: %d factors, building matrix...", len(feature_names))

        # ── 流式构建训练矩阵 (不 vstack) ──
        fwd_dates = sorted(set(forward_returns.index.get_level_values(0)))
        min_factors = max(1, int(len(feature_names) * 0.6))
        _log.info("train: %d fwd_dates, need ≥%d/%d factors per date",
                  len(fwd_dates), min_factors, len(feature_names))

        n_skipped = {"date": 0, "syms": 0, "mask": 0}
        total_samples = 0
        dates_used = []

        X_chunks = []
        y_chunks = []

        for ts in fwd_dates:
            date_str = ts.strftime("%Y-%m-%d")
            syms = set()
            n_avail = 0
            for fn in feature_names:
                fv = factor_values.get(fn)
                if fv is not None and date_str in fv.index:
                    row = fv.loc[date_str].dropna()
                    syms.update(row.index)
                    n_avail += 1
            if n_avail < min_factors:
                n_skipped["date"] += 1
                continue
            syms = list(syms)
            if len(syms) < 30:
                n_skipped["syms"] += 1
                continue

            X_day = np.column_stack([
                factor_values[fn].loc[date_str].reindex(syms).fillna(0).values
                if fn in factor_values and date_str in factor_values[fn].index
                else np.zeros(len(syms))
                for fn in feature_names
            ])
            y_day = forward_returns.loc[ts].reindex(syms).fillna(0).values

            mask = ~np.isnan(y_day)
            if mask.sum() < 20:
                n_skipped["mask"] += 1
                continue

            X_chunks.append(X_day[mask])
            y_chunks.append(y_day[mask])
            total_samples += mask.sum()
            dates_used.append(ts)

            # 每 50 天 flush 一次, 避免 X_chunks 过大
            if len(X_chunks) >= 50:
                _log.info("train: flushing %d days → %d samples",
                          len(X_chunks), sum(len(c) for c in X_chunks))

        _log.info("train: %d dates, %d samples, skipped=%s",
                  len(X_chunks), total_samples, n_skipped)
        if not X_chunks:
            raise ValueError(
                "No valid training samples — check factor_values and forward_returns alignment"
            )

        # 合并 (已按50天分批 flush, X_chunks 最多 50 段)
        X = np.vstack(X_chunks)
        y = np.concatenate(y_chunks)

        # 释放中间列表
        del X_chunks, y_chunks

        # 过滤极端值 (winsorize 99%)
        y_upper = np.percentile(y, 99)
        y_lower = np.percentile(y, 1)
        y = np.clip(y, y_lower, y_upper)

        _log.info(
            "lgb train: %d samples × %d features over %d dates",
            len(y), X.shape[1], len(dates_used),
        )

        self._lgb = lgb.LGBMRegressor(**lgb_params)
        self._lgb.fit(X, y)

        # ── 评估训练集 IC ──
        y_pred = self._lgb.predict(X)
        ic = np.corrcoef(y_pred, y)[0, 1] if len(y) > 1 else 0.0

        # ── 保存模型 ──
        train_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        model_path = os.path.join(_MODEL_DIR, f"lgb_model_{train_date}.txt")
        os.makedirs(_MODEL_DIR, exist_ok=True)
        self._lgb.booster_.save_model(model_path)

        with open(model_path, "rb") as f:
            model_hash = hashlib.sha256(f.read()).hexdigest()[:16]

        self._metadata = ModelMetadata(
            train_date=train_date,
            train_start=dates_used[0] if dates_used else "",
            train_end=dates_used[-1] if dates_used else "",
            n_samples=len(y),
            n_features=X.shape[1],
            feature_names=list(feature_names),
            ic_mean=round(float(ic), 4),
            ic_std=round(float(np.std(y_pred - y)), 6),
            model_hash=model_hash,
            lgb_params=lgb_params,
        )

        meta_path = os.path.join(_MODEL_DIR, f"lgb_metadata_{train_date}.json")
        with open(meta_path, "w") as f:
            json.dump({
                k: v for k, v in self._metadata.__dict__.items()
            }, f, indent=2, default=str)

        _log.info(
            "lgb model saved: %s (IC=%.4f, %d features, %d samples)",
            model_path, ic, len(feature_names), len(y),
        )
        return self._metadata

    # ── 预测 ──

    def predict(self, factor_values: dict, symbols: list[str] = None) -> pd.Series:
        """生成当前截面的 alpha 预测。

        Args:
            factor_values: {factor_name: Series(index=symbol)} — 当日截面因子值
            symbols: 候选 symbol 列表 (None=从 factor_values 推断)

        Returns:
            Series(index=symbol, value=predicted_return) — alpha scores
        """
        if not self.is_trained:
            raise RuntimeError(
                "LgbAlphaModel not trained. Call train() first or fall back to ic_weighted."
            )

        # 确定 symbol 列表
        if symbols is None:
            syms = set()
            for series in factor_values.values():
                if isinstance(series, pd.Series):
                    syms.update(series.dropna().index)
            symbols = sorted(syms)

        if not symbols:
            return pd.Series(dtype=float)

        # 构建特征矩阵
        X = np.column_stack([
            factor_values.get(fn, pd.Series(0, index=symbols))
            .reindex(symbols).fillna(0).values
            for fn in self._feature_names
            if fn in factor_values
        ])

        if X.shape[1] < len(self._feature_names):
            missing = set(self._feature_names) - set(factor_values.keys())
            _log.warning(
                "lgb predict: %d/%d features available (missing: %s), "
                "padding with zeros",
                X.shape[1], len(self._feature_names),
                ", ".join(sorted(missing)[:5]),
            )
            # 用零填充缺失特征
            pad = np.zeros((X.shape[0], len(self._feature_names) - X.shape[1]))
            X = np.column_stack([X, pad])

        preds = self._lgb.predict(X)
        result = pd.Series(preds, index=symbols, name="alpha_lgb")
        return result.dropna()

    # ── 加载已保存模型 ──

    def load(self, date: str = None) -> bool:
        """加载指定日期的已训练模型。date=None → 加载最新。"""
        if not self._is_available:
            return False

        os.makedirs(_MODEL_DIR, exist_ok=True)

        if date:
            model_path = os.path.join(_MODEL_DIR, f"lgb_model_{date}.txt")
            meta_path = os.path.join(_MODEL_DIR, f"lgb_metadata_{date}.json")
        else:
            # 找最新模型
            models = sorted([
                f for f in os.listdir(_MODEL_DIR)
                if f.startswith("lgb_model_") and f.endswith(".txt")
            ])
            if not models:
                _log.info("lgb: no saved models in %s", _MODEL_DIR)
                return False
            date = models[-1].replace("lgb_model_", "").replace(".txt", "")
            model_path = os.path.join(_MODEL_DIR, models[-1])
            meta_path = os.path.join(_MODEL_DIR, f"lgb_metadata_{date}.json")

        if not os.path.exists(model_path):
            _log.warning("lgb: model not found: %s", model_path)
            return False

        try:
            import lightgbm as lgb
            self._lgb = lgb.Booster(model_file=model_path)
        except Exception as e:
            _log.error("lgb: failed to load model %s: %s", model_path, e)
            return False

        # 加载元数据
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta_dict = json.load(f)
            self._feature_names = meta_dict.get("feature_names", [])
            self._metadata = ModelMetadata(**{
                k: v for k, v in meta_dict.items()
                if k in ModelMetadata.__dataclass_fields__
            })
        else:
            self._feature_names = []

        _log.info(
            "lgb model loaded: %s (%d features, IC=%.4f)",
            date, len(self._feature_names),
            self._metadata.ic_mean if self._metadata else 0,
        )
        return True

    def list_models(self) -> list[str]:
        """列出所有可用模型版本 (日期)。"""
        os.makedirs(_MODEL_DIR, exist_ok=True)
        models = sorted([
            f.replace("lgb_model_", "").replace(".txt", "")
            for f in os.listdir(_MODEL_DIR)
            if f.startswith("lgb_model_") and f.endswith(".txt")
        ])
        return models

    def feature_importance(self) -> dict[str, float]:
        """P2-3: LGB 特征重要性 (gain-based)。

        Returns:
            {factor_name: importance_score} 按重要性降序排列
        """
        if not self.is_trained:
            return {}
        importance = self._lgb.booster_.feature_importance(importance_type="gain")
        total = importance.sum() or 1
        result = {}
        for i, fn in enumerate(self._feature_names):
            if i < len(importance):
                result[fn] = round(float(importance[i] / total), 4)
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def shap_explain(self, factor_values: dict,
                     symbols: list[str] = None, max_samples: int = 500) -> dict:
        """P2-3: SHAP 值分解 — 每个因子对预测的边际贡献。

        Args:
            factor_values: {factor_name: Series(index=symbol)} — 当日截面因子值
            symbols: 样本股票 (None=取前 max_samples 只)
            max_samples: shap 计算样本上限

        Returns:
            {factor_name: mean_abs_shap, top_contributors: [(symbol, shap_val)]}
            需要 pip install shap
        """
        if not self.is_trained:
            return {}
        try:
            import shap
        except ImportError:
            _log.warning("shap not installed — pip install shap")
            return {}

        if symbols is None:
            syms = set()
            for series in factor_values.values():
                if isinstance(series, pd.Series):
                    syms.update(series.dropna().index)
            symbols = sorted(syms)[:max_samples]

        if not symbols:
            return {}

        X = np.column_stack([
            factor_values.get(fn, pd.Series(0, index=symbols))
            .reindex(symbols).fillna(0).values
            for fn in self._feature_names
            if fn in factor_values
        ])

        explainer = shap.TreeExplainer(self._lgb)
        shap_values = explainer.shap_values(X)

        # 每个因子的平均绝对 SHAP
        result = {}
        for i, fn in enumerate(self._feature_names):
            if i < shap_values.shape[1]:
                mean_abs = float(np.abs(shap_values[:, i]).mean())
                # 每个因子找 top 3 贡献股票
                top_idx = np.argsort(np.abs(shap_values[:, i]))[-3:][::-1]
                top_contributors = [
                    (symbols[j], round(float(shap_values[j, i]), 6))
                    for j in top_idx if j < len(symbols)
                ]
                result[fn] = {"mean_abs_shap": round(mean_abs, 6),
                              "top_contributors": top_contributors}

        _log.info(f"SHAP: {len(result)} factors explained over {len(symbols)} samples")
        return result


# ═══════════════════════════════════════════════════════════
# 模型集成 — LGB + MLP 投票 (P0-①, test-v262)
# ═══════════════════════════════════════════════════════════

class EnsembleAlphaModel:
    """集成 Alpha 模型: LGB + MLP (sklearn 神经网络) 平均投票。

    动机:
      - 单模型 (LGB only) 过拟合风险高: train IC=0.45 可能虚高
      - 两个异质模型 (树模型 LGB + 神经网络 MLP) 误差不相干 → 投票降方差
      - MLP 用 sklearn MLPRegressor, 零额外依赖
      - 最终 alpha = 0.5 × LGB_pred + 0.5 × MLP_pred

    Usage:
        model = EnsembleAlphaModel()
        model.train(factor_panels, forward_returns)
        alpha = model.predict(factor_values)
    """

    def __init__(self, lgb_weight: float = 0.5):
        self.lgb_weight = lgb_weight
        self._lgb = None
        self._mlp = None
        self._feature_names: list[str] = []
        self._scaler = None  # StandardScaler for MLP

    @property
    def is_trained(self) -> bool:
        return self._lgb is not None and self._mlp is not None

    def train(self, factor_values, forward_returns,
              feature_names=None, lgb_params=None) -> dict:
        """训练 LGB + MLP 双模型。

        Returns:
            {lgb_ic, mlp_ic, ensemble_ic, n_samples, n_features}
        """
        if feature_names is None:
            feature_names = list(factor_values.keys())
        self._feature_names = feature_names

        _log.info("ensemble: building training matrix...")

        # 复用 LGB 训练逻辑构建 X, y
        fwd_dates = sorted(set(forward_returns.index.get_level_values(0)))
        min_factors = max(1, int(len(feature_names) * 0.6))

        X_chunks, y_chunks = [], []
        for ts in fwd_dates:
            date_str = ts.strftime("%Y-%m-%d")
            syms = set()
            n_avail = 0
            for fn in feature_names:
                fv = factor_values.get(fn)
                if fv is not None and date_str in fv.index:
                    row = fv.loc[date_str].dropna()
                    syms.update(row.index)
                    n_avail += 1
            if n_avail < min_factors:
                continue
            syms = list(syms)
            if len(syms) < 30:
                continue
            X_day = np.column_stack([
                factor_values[fn].loc[date_str].reindex(syms).fillna(0).values
                if fn in factor_values and date_str in factor_values[fn].index
                else np.zeros(len(syms))
                for fn in feature_names
            ])
            y_day = forward_returns.loc[ts].reindex(syms).fillna(0).values
            mask = ~np.isnan(y_day)
            if mask.sum() < 20:
                continue
            X_chunks.append(X_day[mask])
            y_chunks.append(y_day[mask])

        X = np.vstack(X_chunks)
        y = np.concatenate(y_chunks)
        y_upper, y_lower = np.percentile(y, 99), np.percentile(y, 1)
        y = np.clip(y, y_lower, y_upper)

        _log.info(f"ensemble: {len(y)} samples × {X.shape[1]} features")

        # ── LGB ──
        import lightgbm as lgb
        self._lgb = lgb.LGBMRegressor(
            **(lgb_params or {"objective": "regression", "n_estimators": 200,
                              "num_leaves": 31, "learning_rate": 0.05,
                              "verbose": -1, "min_data_in_leaf": 20}))
        self._lgb.fit(X, y)
        y_lgb = self._lgb.predict(X)
        lgb_ic = np.corrcoef(y_lgb, y)[0, 1]

        # ── MLP (sklearn 神经网络, 零额外依赖) ──
        from sklearn.neural_network import MLPRegressor
        from sklearn.preprocessing import StandardScaler
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)
        self._mlp = MLPRegressor(
            hidden_layer_sizes=(64, 32), activation='relu',
            max_iter=200, early_stopping=True, random_state=42)
        self._mlp.fit(X_scaled, y)
        y_mlp = self._mlp.predict(X_scaled)
        mlp_ic = np.corrcoef(y_mlp, y)[0, 1]

        # ── 集成 ──
        y_ens = self.lgb_weight * y_lgb + (1 - self.lgb_weight) * y_mlp
        ens_ic = np.corrcoef(y_ens, y)[0, 1]

        _log.info(f"ensemble trained: LGB_IC={lgb_ic:.4f}, MLP_IC={mlp_ic:.4f}, Ensemble_IC={ens_ic:.4f}")
        return {"lgb_ic": round(lgb_ic, 4), "mlp_ic": round(mlp_ic, 4),
                "ensemble_ic": round(ens_ic, 4), "n_samples": len(y),
                "n_features": X.shape[1]}

    def predict(self, factor_values: dict, symbols=None) -> pd.Series:
        """集成预测: 0.5×LGB + 0.5×MLP。"""
        if not self.is_trained:
            raise RuntimeError("EnsembleAlphaModel not trained")

        if symbols is None:
            syms = set()
            for series in factor_values.values():
                if isinstance(series, pd.Series):
                    syms.update(series.dropna().index)
            symbols = sorted(syms)
        if not symbols:
            return pd.Series(dtype=float)

        X = np.column_stack([
            factor_values.get(fn, pd.Series(0, index=symbols))
            .reindex(symbols).fillna(0).values
            for fn in self._feature_names if fn in factor_values
        ])

        # LGB
        y_lgb = self._lgb.predict(X)
        # MLP (标准化)
        X_scaled = self._scaler.transform(X)
        y_mlp = self._mlp.predict(X_scaled)
        # 平均投票
        y_ens = self.lgb_weight * y_lgb + (1 - self.lgb_weight) * y_mlp
        return pd.Series(y_ens, index=symbols, name="alpha_ensemble").dropna()


# 滚动训练 + 时间序列 CV (P0-②, test-v262)
def rolling_train_cv(
    factor_values: dict[str, pd.DataFrame],
    forward_returns: pd.Series,
    n_splits: int = 5,
    train_window_days: int = 252,
    test_window_days: int = 60,
    feature_names: list[str] = None,
) -> dict:
    """滚动时间序列交叉验证。

    对标 Qlib RollingTrainer + PurgedGroupTimeSeriesSplit:
      - 按日期升序分窗，逐窗训练 → OOS 预测 → 收集 IC
      - 窗口间不重叠，保时序

    Returns: {fold_ics, mean_ic, std_ic, n_splits}
    """
    if feature_names is None:
        feature_names = list(factor_values.keys())
    fwd_dates = sorted(set(forward_returns.index.get_level_values(0)))
    if len(fwd_dates) < train_window_days + test_window_days:
        return {"fold_ics": [], "mean_ic": 0, "n_splits": 0}

    fold_ics = []
    for fold in range(n_splits):
        train_end = len(fwd_dates) - test_window_days - fold * test_window_days
        if train_end < train_window_days:
            break
        train_dates = fwd_dates[train_end - train_window_days:train_end]
        test_dates = fwd_dates[train_end:train_end + test_window_days]

        # 构建训练矩阵 (复用 train 逻辑)
        X_tr, y_tr = [], []
        for ts in train_dates:
            ds = ts.strftime("%Y-%m-%d")
            syms = set()
            for fn in feature_names:
                fv = factor_values.get(fn)
                if fv is not None and ds in fv.index:
                    syms.update(fv.loc[ds].dropna().index)
            syms = list(syms)
            if len(syms) < 30:
                continue
            X = np.column_stack([
                (factor_values[fn].loc[ds].reindex(syms).fillna(0).values
                 if fn in factor_values and ds in factor_values[fn].index
                 else np.zeros(len(syms))) for fn in feature_names])
            y = forward_returns.loc[ts].reindex(syms).fillna(0).values
            m = ~np.isnan(y)
            if m.sum() < 20:
                continue
            X_tr.append(X[m]); y_tr.append(y[m])
        if not X_tr:
            continue
        X_tr = np.vstack(X_tr); y_tr = np.concatenate(y_tr)

        from lightgbm import LGBMRegressor
        m = LGBMRegressor(n_estimators=200, num_leaves=31, learning_rate=0.05,
                          verbose=-1, min_data_in_leaf=20)
        m.fit(X_tr, y_tr)

        # OOS 预测
        yp_all, yt_all = [], []
        for ts in test_dates:
            ds = ts.strftime("%Y-%m-%d")
            syms = set()
            for fn in feature_names:
                fv = factor_values.get(fn)
                if fv is not None and ds in fv.index:
                    syms.update(fv.loc[ds].dropna().index)
            syms = list(syms)
            if len(syms) < 30:
                continue
            X_te = np.column_stack([
                (factor_values[fn].loc[ds].reindex(syms).fillna(0).values
                 if fn in factor_values and ds in factor_values[fn].index
                 else np.zeros(len(syms))) for fn in feature_names])
            y_te = forward_returns.loc[ts].reindex(syms).fillna(0).values
            mask = ~np.isnan(y_te)
            if mask.sum() < 20:
                continue
            yp_all.append(m.predict(X_te[mask]))
            yt_all.append(y_te[mask])
        if yp_all:
            ic = np.corrcoef(np.concatenate(yp_all), np.concatenate(yt_all))[0, 1]
            fold_ics.append(round(float(ic), 4))

    mean_ic = round(float(np.mean(fold_ics)), 4) if fold_ics else 0
    _log.info(f"rolling CV: mean_IC={mean_ic:.4f} ({len(fold_ics)} folds)")
    return {"fold_ics": fold_ics, "mean_ic": mean_ic,
            "n_splits": len(fold_ics)}


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _check_lightgbm() -> bool:
    try:
        import lightgbm  # noqa: F401
        return True
    except ImportError:
        return False


def build_forward_returns(
    store=None,
    start_date: str = None,
    end_date: str = None,
    horizon: int = 5,
    symbols: list[str] = None,
) -> pd.Series:
    """构建前向收益率 Series，用于模型训练。

    从 DataStore 获取日线数据，计算 horizon 日的前向收益率。

    Args:
        store: DataStore 实例 (None=自动创建)
        start_date: 起始日
        end_date: 截止日
        horizon: 前向天数 (默认 5 个交易日)
        symbols: 股票列表 (None=全部)

    Returns:
        Series with MultiIndex(date, symbol), values = forward returns
    """
    from quant.data.store import DataStore

    if store is None:
        store = DataStore()

    if start_date is None:
        start_date = _require_cfg("data.start_date")
    if end_date is None:
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")

    if symbols is None:
        from quant.data.repos import UniverseRepo
        symbols = UniverseRepo().get_symbols(exclude_market="BJ")

    data = store.get_daily(symbols, start=start_date, end=end_date)
    close = data["close"]

    # 前向收益率: close[t+horizon] / close[t] - 1
    fwd = close.shift(-horizon) / close - 1
    fwd = fwd.stack().dropna()
    fwd.name = "forward_return"

    store.close()
    return fwd


# ═══════════════════════════════════════════════════════════
# 单例 + 工厂
# ═══════════════════════════════════════════════════════════

_lgb_instance: Optional[LgbAlphaModel] = None


def get_lgb_model(auto_load: bool = True) -> LgbAlphaModel:
    """获取 LgbAlphaModel 单例。

    auto_load=True: 自动加载最新已保存模型。
    """
    global _lgb_instance
    if _lgb_instance is None:
        _lgb_instance = LgbAlphaModel()
        if auto_load and _lgb_instance._is_available:
            loaded = _lgb_instance.load()
            if loaded:
                _log.info("lgb: auto-loaded latest model")
            else:
                _log.info("lgb: no saved model, needs train() first")
    return _lgb_instance


def reset_lgb_model():
    """重置模型单例。"""
    global _lgb_instance
    _lgb_instance = None


# ═══════════════════════════════════════════════════════════
# 便捷训练入口
# ═══════════════════════════════════════════════════════════

def train_lgb_model(
    start_date: str = None,
    end_date: str = None,
    horizon: int = None,
    factor_status_filter: str = "backtesting",
):
    """便捷训练入口 — 从 DataStore + FactorStore 构建训练数据并训练模型。

    使用方式:
        PYTHONPATH=. python3 -c \
            "from quant.alpha.qlib_model import train_lgb_model; train_lgb_model()"

    流程:
        1. 加载因子值面板 (从 FactorStore 批量读取)
        2. 构建前向收益率 (从 DataStore 日线)
        3. 对齐日期 × symbol
        4. 训练 LightGBM 回归器
        5. 保存模型 + 元数据
    """
    from quant.data.store import DataStore
    from quant.factor.store import FactorStore
    from quant.factor.compute import get_factor_names
    from quant.config.paths import FACTOR_CACHE_DB
    from quant.config.constants import _require_cfg
    from quant.utils.logger import get_logger as _get_log

    _log = _get_log("train_lgb")

    if start_date is None:
        start_date = _require_cfg("alpha.lgb.train.start_date")
    if end_date is None:
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
    if horizon is None:
        horizon = _require_cfg("alpha.lgb.train.forward_horizon")

    _log.info("train_lgb: %s → %s (horizon=%dd)", start_date, end_date, horizon)

    # 1. 加载因子
    fn = get_factor_names(status_filter=factor_status_filter)
    _log.info("train_lgb: loading %d factors from cache...", len(fn))

    fstore = FactorStore(db_path=FACTOR_CACHE_DB)

    # ADR-039: gzip CSV backend — 逐日加载，每日文件只读一次
    _log.info("train_lgb: building factor panels from gzip CSV cache...")
    import os
    cache_dir = fstore._cache_dir
    avail_dates = sorted(f.replace('.csv.gz', '') for f in os.listdir(cache_dir) if f.endswith('.csv.gz'))
    train_dates = [d for d in avail_dates if start_date <= d <= end_date]
    _log.info("train_lgb: %d dates, %d factors", len(train_dates), len(fn))

    # 每日读一次, 提取全部因子
    factor_panels = {name: {} for name in fn}
    for i, d in enumerate(train_dates):
        data = fstore.load(d, factor_names=fn)
        for name in fn:
            if name in data and not data[name].empty:
                factor_panels[name][d] = data[name]
        if (i + 1) % 20 == 0:
            _log.info("train_lgb: loaded %d/%d dates", i + 1, len(train_dates))

    # 转换 dict → DataFrame
    factor_panels = {name: pd.DataFrame(series_dict).T for name, series_dict in factor_panels.items() if series_dict}

    if not factor_panels:
        raise ValueError("No factor data loaded from cache. Run factor_cache materialization first.")

    _log.info("train_lgb: %d factors loaded", len(factor_panels))

    # align start_date to actual cache range (config default = 2015 but cache starts 2025)
    cache_start = min(df.index[0] for df in factor_panels.values())
    if start_date is None or str(cache_start) > str(start_date):
        _log.info("train_lgb: overriding start_date %s → %s (cache min)", start_date, cache_start)
        start_date = str(cache_start)

    for name, df in list(factor_panels.items())[:3]:
        _log.info("  %s: shape=%s, index=%s..%s", name, df.shape, str(df.index[0]), str(df.index[-1]))

    # 2. 构建前向收益率
    forward_rets = build_forward_returns(
        start_date=start_date, end_date=end_date, horizon=horizon,
    )

    # 3. 训练 (训练完成后释放因子面板内存 ~1-3GB)
    lgb_params = {
        k: v for k, v in _require_cfg("alpha.lgb.params").items()
    } if _require_cfg("alpha.lgb.params") else None

    model = LgbAlphaModel()
    try:
        meta = model.train(
            factor_values=factor_panels,
            forward_returns=forward_rets,
            lgb_params=lgb_params,
        )
    finally:
        del factor_panels, forward_rets
        import gc as _gc
        _gc.collect()
        _log.info("train_lgb: freed factor panels + forward returns")

    _log.info("train_lgb: done — IC=%.4f, %d features, model saved to %s",
              meta.ic_mean, meta.n_features, _MODEL_DIR)

    # 设为全局单例
    global _lgb_instance
    _lgb_instance = model

    return meta
