"""XGBoost Alpha 预测模型.

ADR-??? (待编号): XGBoost 作为 LightGBM 的替代/补充 ML 后端.

用途:
  - XGBoost 在小样本 (<10万行) 上通常比 LightGBM 更稳定
  - 特征重要性更稳定, 适合最终 Alpha 合成
  - 与 LgbAlphaModel API 完全兼容, AlphaModel.combine() 可通过
    combine_mode='xgb' 切换

模型生命周期:
  1. train()    — 历史因子值 + 前向收益 → 训练 XGBRegressor
  2. predict()  — 当日因子值 → 预期收益 (alpha scores)
  3. 无模型时    — 自动回退到 IC 加权合成

存储: quant/data/models/ 目录
  xgb_model_2026-07-28.json    (XGBoost 原生 JSON 格式)
  xgb_metadata_2026-07-28.json (训练元数据)
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

_log = get_logger("alpha.xgb_model")

_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "models"
)


@dataclass
class ModelMetadata:
    """模型元数据 — 记录训练信息, 用于版本追踪和审计."""
    train_date: str
    train_start: str
    train_end: str
    n_samples: int
    n_features: int
    feature_names: list[str]
    ic_mean: float
    ic_std: float
    model_hash: str
    xgb_params: dict = field(default_factory=dict)


class XgbAlphaModel:
    """XGBoost Alpha 预测模型.

    可用作 AlphaModel 的 combine_mode 后端, 通过 predict() 输出 alpha scores.
    未训练时 get_predictor() 抛异常, 调用方应回退到 ic_weighted 等线性方法.
    """

    def __init__(self):
        self._xgb = None
        self._feature_names: list[str] = []
        self._metadata: Optional[ModelMetadata] = None
        self._is_available = _check_xgboost()

    @property
    def is_trained(self) -> bool:
        return self._xgb is not None and len(self._feature_names) > 0

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
        xgb_params: dict = None,
    ) -> ModelMetadata:
        """训练 XGBoost 回归模型.

        Args:
            factor_values: {factor_name: DataFrame(index=date, columns=symbols)}
            forward_returns: Series(index=MultiIndex(date, symbol))
            feature_names: 使用的因子名列表 (None=全部)
            xgb_params: XGBoost 参数 (None=默认配置)

        Returns:
            ModelMetadata
        """
        if not self._is_available:
            raise ImportError("xgboost not installed. Install: pip install xgboost")

        import xgboost as xgb

        if xgb_params is None:
            xgb_params = dict(_require_cfg("alpha.xgb.params"))

        if feature_names is None:
            feature_names = list(factor_values.keys())

        self._feature_names = feature_names

        _log.info("xgb train: %d factors, building matrix...", len(feature_names))

        fwd_dates = sorted(set(forward_returns.index.get_level_values(0)))
        min_factors = max(1, int(len(feature_names) * 0.6))
        _log.info("xgb train: %d fwd_dates, need >=%d/%d factors per date",
                  len(fwd_dates), min_factors, len(feature_names))

        n_skipped = {"date": 0, "syms": 0, "mask": 0}
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
            ]).astype(np.float32)
            y_day = forward_returns.loc[ts].reindex(syms).fillna(0).values.astype(np.float32)

            mask = ~np.isnan(y_day)
            if mask.sum() < 20:
                n_skipped["mask"] += 1
                continue

            X_chunks.append(X_day[mask])
            y_chunks.append(y_day[mask])

            if len(X_chunks) >= 50:
                import gc
                _log.info("xgb train: flushing %d days", len(X_chunks))
                gc.collect()

        _log.info("xgb train: skipped=%s", n_skipped)
        if not X_chunks:
            raise ValueError("No valid training samples")

        X = np.vstack(X_chunks).astype(np.float32)
        y = np.concatenate(y_chunks).astype(np.float32)
        del X_chunks, y_chunks

        # winsorize 99%
        y_upper = np.percentile(y, 99)
        y_lower = np.percentile(y, 1)
        y = np.clip(y, y_lower, y_upper)

        _log.info("xgb train: %d samples x %d features", len(y), X.shape[1])

        # 分块训练: XGBoost 支持 xgb_model 参数继续训练
        train_chunk_samples = _require_cfg("alpha.xgb.train.train_chunk_samples")
        n_total = len(y)
        self._xgb = None

        for batch_start in range(0, n_total, train_chunk_samples):
            batch_end = min(batch_start + train_chunk_samples, n_total)
            batch_n = batch_end - batch_start
            _log.info("xgb train: batch %d-%d/%d (%.1fM samples)",
                      batch_start, batch_end, n_total, batch_n / 1e6)

            model = xgb.XGBRegressor(**xgb_params)
            model.fit(
                X[batch_start:batch_end],
                y[batch_start:batch_end],
                xgb_model=self._xgb.get_booster() if self._xgb is not None else None,
                verbose=False,
            )
            self._xgb = model

        # 训练集 IC
        y_pred_chunks = []
        for batch_start in range(0, n_total, train_chunk_samples):
            batch_end = min(batch_start + train_chunk_samples, n_total)
            y_pred_chunks.append(self._xgb.predict(X[batch_start:batch_end]))
        y_pred = np.concatenate(y_pred_chunks)
        ic = np.corrcoef(y_pred, y)[0, 1] if len(y) > 1 else 0.0
        ic_std = round(float(np.std(y_pred - y)), 6)

        del X, y, y_pred, y_pred_chunks

        # 保存模型
        train_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        os.makedirs(_MODEL_DIR, exist_ok=True)
        model_path = os.path.join(_MODEL_DIR, f"xgb_model_{train_date}.json")
        self._xgb.save_model(model_path)

        with open(model_path, "rb") as f:
            model_hash = hashlib.sha256(f.read()).hexdigest()[:16]

        self._metadata = ModelMetadata(
            train_date=train_date,
            train_start=fwd_dates[0].strftime("%Y-%m-%d") if fwd_dates else "",
            train_end=fwd_dates[-1].strftime("%Y-%m-%d") if fwd_dates else "",
            n_samples=n_total,
            n_features=X.shape[1] if 'X' in dir() else len(feature_names),
            feature_names=list(feature_names),
            ic_mean=round(float(ic), 4),
            ic_std=ic_std,
            model_hash=model_hash,
            xgb_params=xgb_params,
        )

        meta_path = os.path.join(_MODEL_DIR, f"xgb_metadata_{train_date}.json")
        with open(meta_path, "w") as f:
            json.dump({k: v for k, v in self._metadata.__dict__.items()},
                      f, indent=2, default=str)

        _log.info("xgb model saved: %s (IC=%.4f, %d features, %d samples)",
                  model_path, ic, len(feature_names), n_total)
        return self._metadata

    # ── 预测 ──

    def predict(self, factor_values: dict, symbols: list[str] = None) -> pd.Series:
        """生成当前截面 alpha 预测."""
        if not self.is_trained:
            raise RuntimeError("XgbAlphaModel not trained")

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
            for fn in self._feature_names
            if fn in factor_values
        ])

        if X.shape[1] < len(self._feature_names):
            missing = set(self._feature_names) - set(factor_values.keys())
            _log.warning("xgb predict: %d/%d features available (missing: %s), padding zeros",
                         X.shape[1], len(self._feature_names), ", ".join(sorted(missing)[:5]))
            pad = np.zeros((X.shape[0], len(self._feature_names) - X.shape[1]))
            X = np.column_stack([X, pad])

        preds = self._xgb.predict(X)
        return pd.Series(preds, index=symbols, name="alpha_xgb").dropna()

    # ── 加载 ──

    def load(self, date: str = None) -> bool:
        """加载指定日期模型. date=None -> 最新."""
        if not self._is_available:
            return False

        import xgboost as xgb
        os.makedirs(_MODEL_DIR, exist_ok=True)

        if date:
            model_path = os.path.join(_MODEL_DIR, f"xgb_model_{date}.json")
            meta_path = os.path.join(_MODEL_DIR, f"xgb_metadata_{date}.json")
        else:
            models = sorted([
                f for f in os.listdir(_MODEL_DIR)
                if f.startswith("xgb_model_") and f.endswith(".json")
            ])
            if not models:
                _log.info("xgb: no saved models")
                return False
            date = models[-1].replace("xgb_model_", "").replace(".json", "")
            model_path = os.path.join(_MODEL_DIR, models[-1])
            meta_path = os.path.join(_MODEL_DIR, f"xgb_metadata_{date}.json")

        if not os.path.exists(model_path):
            _log.warning("xgb: model not found: %s", model_path)
            return False

        try:
            self._xgb = xgb.XGBRegressor()
            self._xgb.load_model(model_path)
        except Exception as e:
            _log.error("xgb: failed to load %s: %s", model_path, e)
            return False

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

        _log.info("xgb model loaded: %s (%d features, IC=%.4f)",
                  date, len(self._feature_names),
                  self._metadata.ic_mean if self._metadata else 0)
        return True

    def list_models(self) -> list[str]:
        """列出所有可用模型版本."""
        os.makedirs(_MODEL_DIR, exist_ok=True)
        return sorted([
            f.replace("xgb_model_", "").replace(".json", "")
            for f in os.listdir(_MODEL_DIR)
            if f.startswith("xgb_model_") and f.endswith(".json")
        ])

    def feature_importance(self) -> dict[str, float]:
        """XGBoost 特征重要性 (gain-based)."""
        if not self.is_trained:
            return {}
        importance = self._xgb.get_booster().get_score(importance_type="gain")
        total = sum(importance.values()) or 1
        # 将 f0, f1, ... 映射回因子名
        result = {}
        for raw_name, score in importance.items():
            # raw_name 形如 'f0'
            try:
                idx = int(raw_name.replace("f", ""))
                if idx < len(self._feature_names):
                    result[self._feature_names[idx]] = round(float(score / total), 4)
            except ValueError:
                result[raw_name] = round(float(score / total), 4)
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _check_xgboost() -> bool:
    try:
        import xgboost  # noqa: F401
        return True
    except ImportError:
        return False


# 复用 qlib_model 的 build_forward_returns
from quant.alpha.qlib_model import build_forward_returns  # noqa: E402


# ═══════════════════════════════════════════════════════════
# 单例 + 工厂
# ═══════════════════════════════════════════════════════════

_xgb_instance: Optional[XgbAlphaModel] = None


def get_xgb_model(auto_load: bool = True) -> XgbAlphaModel:
    """获取 XgbAlphaModel 单例."""
    global _xgb_instance
    if _xgb_instance is None:
        _xgb_instance = XgbAlphaModel()
        if auto_load and _xgb_instance._is_available:
            loaded = _xgb_instance.load()
            if loaded:
                _log.info("xgb: auto-loaded latest model")
            else:
                _log.info("xgb: no saved model, needs train() first")
    return _xgb_instance


def reset_xgb_model():
    """重置模型单例."""
    global _xgb_instance
    _xgb_instance = None


# ═══════════════════════════════════════════════════════════
# 便捷训练入口
# ═══════════════════════════════════════════════════════════

def train_xgb_model(
    start_date: str = None,
    end_date: str = None,
    horizon: int = None,
    factor_status_filter: str = "backtesting",
):
    """便捷训练入口 — 从 DataStore + FactorStore 构建训练数据并训练 XGBoost 模型.

    用法:
        PYTHONPATH=. .venv/bin/python -c \
            "from quant.alpha.xgb_model import train_xgb_model; train_xgb_model()"
    """
    from quant.data.store import DataStore
    from quant.factor.store import FactorStore
    from quant.factor.compute import get_factor_names
    from quant.config.paths import FACTOR_CACHE_DB

    if start_date is None:
        start_date = _require_cfg("alpha.xgb.train.start_date")
    if end_date is None:
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
    if horizon is None:
        horizon = _require_cfg("alpha.xgb.train.forward_horizon")

    _log.info("train_xgb: %s -> %s (horizon=%dd)", start_date, end_date, horizon)

    fn = get_factor_names(status_filter=factor_status_filter)
    _log.info("train_xgb: loading %d factors from cache...", len(fn))

    fstore = FactorStore(db_path=FACTOR_CACHE_DB)
    cache_dir = fstore._cache_dir
    avail_dates = sorted(
        f.replace(".csv.gz", "") for f in os.listdir(cache_dir) if f.endswith(".csv.gz")
    )
    train_dates = [d for d in avail_dates if start_date <= d <= end_date]
    _log.info("train_xgb: %d dates, %d factors", len(train_dates), len(fn))

    factor_panels = {name: {} for name in fn}
    for i, d in enumerate(train_dates):
        data = fstore.load(d, factor_names=fn)
        for name in fn:
            if name in data and not data[name].empty:
                factor_panels[name][d] = data[name]
        if (i + 1) % 20 == 0:
            _log.info("train_xgb: loaded %d/%d dates", i + 1, len(train_dates))

    factor_panels = {
        name: pd.DataFrame(series_dict).T.astype("float32")
        for name, series_dict in factor_panels.items() if series_dict
    }

    if not factor_panels:
        raise ValueError("No factor data loaded. Run factor_cache materialization first.")

    cache_start = min(df.index[0] for df in factor_panels.values())
    if start_date is None or str(cache_start) > str(start_date):
        _log.info("train_xgb: overriding start_date %s -> %s", start_date, cache_start)
        start_date = str(cache_start)

    forward_rets = build_forward_returns(
        start_date=start_date, end_date=end_date, horizon=horizon,
    )

    xgb_params = dict(_require_cfg("alpha.xgb.params")) if _require_cfg("alpha.xgb.params") else None

    model = XgbAlphaModel()
    try:
        meta = model.train(
            factor_values=factor_panels,
            forward_returns=forward_rets,
            xgb_params=xgb_params,
        )
    finally:
        del factor_panels, forward_rets
        import gc
        gc.collect()
        _log.info("train_xgb: freed factor panels")

    _log.info("train_xgb: done — IC=%.4f, %d features, model saved to %s",
              meta.ic_mean, meta.n_features, _MODEL_DIR)

    global _xgb_instance
    _xgb_instance = model
    return meta
