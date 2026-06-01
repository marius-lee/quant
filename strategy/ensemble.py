"""模型集成 — 3树模型: LightGBM + XGBoost + ExtraTrees

按尾部IC(70%)+全局IC(30%)综合加权。
去掉 sklearn RandomForest — 大数据集上太慢(12分钟 vs 其他3模型各10-25秒)
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.preprocessing import StandardScaler

from utils.logger import get_logger

logger = get_logger("strategy.ensemble")


class EnsembleModel:
    """激进树模型集成 (3树模型, 无线性基线)"""

    def __init__(self, tail_ic_pct: float = 0.05):
        self.models = []
        self.weights = []
        self.scaler = StandardScaler()
        self.feature_names = None
        self.tail_ic_pct = tail_ic_pct  # 尾部IC: 只评估Top N%收益的排序

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: list = None):
        self.feature_names = feature_names

        n = len(X)
        split = int(n * 0.7)
        X_tr_raw, y_tr = X[:split], y[:split]
        X_va_raw, y_va = X[split:], y[split:]

        X_tr = self.scaler.fit_transform(X_tr_raw)
        X_va = self.scaler.transform(X_va_raw)

        def _tail_ic(pred, y_true, top_pct=0.05):
            """尾部IC: 只评估模型对Top收益股票的排序能力"""
            if len(y_true) < 20:
                return 0
            n_top = max(1, int(len(y_true) * top_pct))
            top_mask = y_true >= np.sort(y_true)[-n_top]
            if top_mask.sum() < 2:
                return 0
            ic = np.corrcoef(pred[top_mask], y_true[top_mask])[0, 1]
            return ic if not np.isnan(ic) else 0

        def _score(model, name):
            """训练模型，返回 (name, model, weight) 或 None"""
            try:
                model.fit(X_tr, y_tr)
                pred = model.predict(X_va)
                ic = np.corrcoef(pred, y_va)[0, 1] if len(y_va) > 1 else 0
                t_ic = _tail_ic(pred, y_va, self.tail_ic_pct)
                if np.isnan(ic) and np.isnan(t_ic):
                    logger.warning(f"model {name}: IC=NaN, excluding")
                    return None
                # 综合权重: 70%尾部IC + 30%全局IC (优先捕捉极端收益)
                combined = 0.7 * abs(t_ic if not np.isnan(t_ic) else 0) + \
                           0.3 * abs(ic if not np.isnan(ic) else 0)
                w = max(0.03, combined)
                logger.info(f"model {name}: global_IC={ic:.4f}, tail_IC={t_ic:.4f}, weight={w:.4f}")
                return (name, model, w)
            except Exception:
                logger.warning(f"model {name} training failed: ", exc_info=True)
                return None

        lgb_model = self._try_lightgbm()
        xgb_model = self._try_xgboost()

        candidates = [
            # 3模型集成: LightGBM(直方图)+XGBoost(列块)+ExtraTrees(随机分裂)
            # 去掉 sklearn RandomForest: 36万样本×200树×深15 = 12分钟, 不可接受
            # 参考: https://dev.to/smarteco/i-benchmarked-8-ml-models-on-cpu-no-tuning-no-tricks-heres-what-happened-1bai
            ("LightGBM", lgb_model),
            ("XGBoost", xgb_model),
            ("ExtraTrees", ExtraTreesRegressor(
                n_estimators=80, max_depth=12, min_samples_leaf=10,
                random_state=42, n_jobs=-1, bootstrap=True, max_samples=0.15,
            )),
        ]

        for name, model in candidates:
            if model is not None:
                result = _score(model, name)
                if result is not None:
                    self.models.append((result[0], result[1]))
                    self.weights.append(result[2])

        if sum(self.weights) > 0:
            self.weights = [w / sum(self.weights) for w in self.weights]

    def _try_lightgbm(self):
        try:
            import lightgbm as lgb
            return lgb.LGBMRegressor(
                n_estimators=200, max_depth=12, learning_rate=0.08,
                num_leaves=63, subsample=0.8, random_state=42, verbose=-1,
            )
        except Exception:
            return None

    def _try_xgboost(self):
        try:
            import xgboost as xgb
            return xgb.XGBRegressor(
                n_estimators=200, max_depth=12, learning_rate=0.08,
                subsample=0.8, random_state=42,
            )
        except Exception:
            return None

    def predict(self, X: np.ndarray) -> np.ndarray:
        """加权平均预测"""
        X = self.scaler.transform(X)
        preds = np.zeros(len(X))
        valid_weight_sum = 0.0
        for (name, model), w in zip(self.models, self.weights):
            p = model.predict(X)
            if np.any(np.isnan(p)):
                logger.warning(f"NaN predictions from {name}, excluding from ensemble")
                continue
            preds += w * p
            valid_weight_sum += w
        if valid_weight_sum > 0:
            preds /= valid_weight_sum
        return preds

    def feature_importance(self) -> pd.Series:
        """加权平均所有模型的因子重要性"""
        if self.feature_names is None:
            return pd.Series()
        total_w = sum(self.weights)
        if total_w == 0:
            return pd.Series()
        imp = np.zeros(len(self.feature_names))
        for (name, model), w in zip(self.models, self.weights):
            if hasattr(model, "feature_importances_"):
                imp += w * model.feature_importances_
            elif hasattr(model, "coef_"):
                # 预留: 线性模型（如 Ridge/LinearRegression）使用系数绝对值作为重要性
                imp += w * np.abs(model.coef_).flatten()
        imp /= total_w
        return pd.Series(imp, index=self.feature_names).sort_values(ascending=False)

    @property
    def model_info(self) -> str:
        return ", ".join(
            f"{name}({w:.2f})" for (name, _), w in zip(self.models, self.weights)
        )


if __name__ == "__main__":
    np.random.seed(42)
    X = np.random.randn(5000, 20)
    true_w = np.random.randn(20)
    y = X @ true_w + np.random.randn(5000) * 2

    em = EnsembleModel()
    em.fit(X, y, [f"f{i}" for i in range(20)])
    print(f"集成模型: {em.model_info}")
    print(f"Top 5 因子: {em.feature_importance().head().to_dict()}")
