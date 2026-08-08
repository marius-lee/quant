"""v423: ML 训练特征统一 + OOS 评估 — ml_common 共享构件.

覆盖:
  1. build_cross_sectional_factors: 逐日截面 z-score (rank→norm), NaN 保留
  2. split_train_oos: 时间顺序 85/15 切分, str/str 比较兼容
  3. daily_ic_series: 逐日 IC 序列 → ic_mean/ic_std/icir/n_days, 每日<20 只剔除
  4. build_train_matrices: 切分 + 面板字符索引对齐 + 缺失因子零填充
  5. ModelMetadata: 新 OOS 字段默认值兼容 (load 兼容旧元数据 JSON)
"""
import logging

import numpy as np
import pandas as pd
import pytest

from quant.alpha import ml_common
from quant.alpha.ml_common import (
    build_cross_sectional_factors,
    split_train_oos,
    daily_ic_series,
    build_train_matrices,
)
from quant.alpha.qlib_model import ModelMetadata
from quant.alpha import qlib_model
from quant.alpha import xgb_model


@pytest.fixture(autouse=True)
def _silence():
    logging.getLogger("alpha.ml_common").setLevel(logging.CRITICAL + 1)
    logging.getLogger("alpha.qlib_model").setLevel(logging.CRITICAL + 1)


def _mk_panel(n_days=40, n_syms=50, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    syms = [f"{i:06d}" for i in range(n_syms)]
    idx = pd.MultiIndex.from_product([dates, syms])
    fwd = pd.Series(rng.normal(0, 0.02, len(idx)), index=idx)
    panels = {
        "f1": pd.DataFrame(rng.normal(0, 1, (n_days, n_syms)),
                           index=[str(d)[:10] for d in dates], columns=syms),
        "f2": pd.DataFrame(rng.normal(0, 1, (n_days, n_syms)),
                           index=[str(d)[:10] for d in dates], columns=syms),
    }
    panels["f1"].iloc[0, :5] = np.nan
    return panels, fwd


class TestCrossSectionalFactors:
    def test_zscore_standard_normal(self):
        panels = {
            "f": pd.DataFrame(
                [[1, 2, 3, 4, 5], [5, 4, 3, 2, 1]],
                index=["2024-01-01", "2024-01-02"],
                columns=list("abcde"),
            )
        }
        z = build_cross_sectional_factors(panels)["f"]
        assert abs(z.loc["2024-01-01", "c"] - z.loc["2024-01-01", "b"]) > 0
        assert z.loc["2024-01-01", "a"] < z.loc["2024-01-01", "c"] < z.loc["2024-01-01", "e"]

    def test_nan_preserved(self):
        panels = {
            "f": pd.DataFrame(
                [[1.0, np.nan, 2.0]],
                index=["2024-01-01"], columns=["a", "b", "c"],
            )
        }
        zs = build_cross_sectional_factors(panels)["f"]
        assert np.isnan(zs.loc["2024-01-01", "b"])

    def test_empty_panel_passthrough(self):
        panels = {"f": pd.DataFrame()}
        zs = build_cross_sectional_factors(panels)["f"]
        assert zs.empty


class TestSplitTrainOos:
    def test_ratio_and_disjoint(self):
        dates = [f"2024-01-{i:02d}" for i in range(1, 21)]
        tr, oo = split_train_oos(dates, oos_frac=0.15)
        assert len(tr) == 17
        assert len(oo) == 3
        assert tr.isdisjoint(oo)

    def test_timestamp_input(self):
        dates = pd.bdate_range("2024-01-01", periods=10)
        tr, oo = split_train_oos(dates, oos_frac=0.5)
        assert len(oo) == 5
        assert all(isinstance(d, str) for d in tr | oo)

    def test_empty(self):
        assert split_train_oos([]) == (set(), set())


class TestDailyIcSeries:
    def test_basic(self):
        n_days, n_syms = 5, 30
        rng = np.random.default_rng(1)
        preds, acts, days = [], [], []
        for i in range(n_days):
            p = rng.normal(0, 1, n_syms)
            a = 0.5 * p + rng.normal(0, 0.1, n_syms)
            for j in range(n_syms):
                preds.append(p[j]); acts.append(a[j]); days.append(f"2024-01-0{i+1}")
        res = daily_ic_series(np.array(preds), np.array(acts), days)
        assert res["n_days"] == 5
        assert res["ic_mean"] > 0.3
        assert res["icir"] > 0.5

    def test_too_few_per_day_skipped(self):
        res = daily_ic_series(np.array([1.0, 2.0]), np.array([1.0, 2.0]),
                              ["2024-01-01", "2024-01-01"])
        assert res == {"ic_mean": 0.0, "ic_std": 0.0, "icir": 0.0, "n_days": 0}

    def test_constant_skipped(self):
        res = daily_ic_series(np.array([1.0] * 25), np.random.rand(25),
                              ["2024-01-01"] * 25)
        assert res["n_days"] == 0


class TestBuildTrainMatrices:
    def test_matches_forward_dates(self):
        panels, fwd = _mk_panel(n_days=40)
        mats = build_train_matrices(panels, fwd, ["f1", "f2"], oos_frac=0.15)
        assert mats["X_tr"].shape[0] == len(mats["y_tr"])
        assert mats["X_oo"].shape[0] == len(mats["y_oo"])
        assert mats["X_tr"].shape[1] == 2
        assert len(mats["oos_dates"]) == len(mats["y_oo"])
        assert len(mats["train_dates"]) > 0
        assert mats["y_tr"].dtype == np.float32

    def test_missing_factor_date_zero_filled(self):
        panels, fwd = _mk_panel(n_days=40)
        panels["f2"] = panels["f2"].iloc[:10]  # 截断: 只有前 10 天
        mats = build_train_matrices(panels, fwd, ["f1", "f2"], oos_frac=0.15)
        assert mats["X_tr"].shape[1] == 2
        assert np.allclose(mats["X_tr"][:, 1], 0) is False  # 前段有值

    def test_no_data_raises(self):
        panels, fwd = _mk_panel(n_days=5)
        panels = {"f1": panels["f1"].iloc[:0]}
        with pytest.raises(ValueError):
            build_train_matrices(panels, fwd, ["f1"], oos_frac=0.5)


class TestModelMetadataCompat:
    def test_old_meta_dict_loads(self, tmp_path):
        """旧元数据 JSON 无 OOS 字段 → load() 默认 0, 不崩."""
        old = {
            "train_date": "2024-01-01",
            "n_samples": 100,
            "n_features": 2,
            "feature_names": ["f1"],
            "ic_mean": 0.1,
            "ic_std": 0.0,
            "model_hash": "abc",
            "lgb_params": {},
        }
        md = ModelMetadata(**{k: v for k, v in old.items()
                              if k in ModelMetadata.__dataclass_fields__})
        assert md.oos_ic_mean == 0.0
        assert md.oos_icir == 0.0
        assert md.oos_n_days == 0
        md2 = ModelMetadata(**{k: v for k, v in old.items()
                               if k in ModelMetadata.__dataclass_fields__})
        assert md2.oos_ic_mean == 0.0

    def test_metadata_fields_exist_both_models(self):
        for mod in (qlib_model, xgb_model):
            fields = set(getattr(mod, "ModelMetadata").__dataclass_fields__)
            assert {"oos_ic_mean", "oos_icir", "oos_n_days", "train_start", "train_end"} <= fields