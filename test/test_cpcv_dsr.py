"""§8.4 (test-v300): CPCV+DSR 因子健康评估.

覆盖:
  - cpcv_oos_series: fold 切分/OOS 拼接/边界
  - evaluate_factor: 强 IC → significant, 噪声 → degraded, 不足 → insufficient
  - config 键接线 (attribution.dsr_* / cpcv_*)
"""
import numpy as np
import pytest

from quant.evaluation.cpcv_dsr import cpcv_oos_series, evaluate_factor


def _ic_series(n, mean, sd=0.05, seed=11):
    rng = np.random.default_rng(seed)
    return rng.normal(mean, sd, n).tolist()


class TestCpcvOosSeries:
    def test_oos_excludes_first_fold(self):
        """100 天 5 组 → 首 fold 无训练段被跳过, OOS = 后 80 天."""
        ics = _ic_series(100, 0.01)
        oos = cpcv_oos_series(ics, n_groups=5, embargo_days=1)
        assert len(oos) == 80
        assert oos.iloc[0] == ics[20]  # 首个 OOS 点 = 第 2 组首日

    def test_oos_preserves_order_and_values(self):
        ics = _ic_series(120, 0.0)
        oos = cpcv_oos_series(ics, n_groups=5, embargo_days=1)
        assert list(oos.values) == ics[24:]  # 120/5=24, 后 4 组全收

    def test_too_few_points_returns_empty(self):
        assert len(cpcv_oos_series([0.01, 0.02, 0.03])) == 0

    def test_nan_dropped(self):
        ics = _ic_series(60, 0.0)
        ics[10] = float("nan")
        oos = cpcv_oos_series(ics, n_groups=5, embargo_days=1)
        assert oos.notna().all()


class TestEvaluateFactor:
    KW = dict(min_days=40, degraded_threshold=0.5, recover_threshold=0.95,
              n_groups=5, embargo_days=1)

    def test_strong_ic_significant(self):
        """稳定正 IC (ICIR 高) → DSR ≈ 1 → significant (恢复候选)."""
        v = evaluate_factor(_ic_series(120, mean=0.03, sd=0.04), n_trials=10, **self.KW)
        assert v["verdict"] == "significant"
        assert v["dsr"] >= 0.95
        assert v["oos_icir"] > 0
        assert v["n_obs"] == 96  # 120 - 120/5

    def test_noise_degraded(self):
        """零均值噪声 → DSR < 0.5 → degraded."""
        v = evaluate_factor(_ic_series(120, mean=0.0, sd=0.05), n_trials=10, **self.KW)
        assert v["verdict"] == "degraded"
        assert v["dsr"] < 0.5

    def test_negative_ic_degraded(self):
        v = evaluate_factor(_ic_series(120, mean=-0.02, sd=0.04), n_trials=10, **self.KW)
        assert v["verdict"] == "degraded"

    def test_insufficient_data_not_penalized(self):
        """30 天 → OOS ~24 < min_days → insufficient, dsr=None, 不罚."""
        v = evaluate_factor(_ic_series(30, mean=0.05), n_trials=10, **self.KW)
        assert v["verdict"] == "insufficient"
        assert v["dsr"] is None

    def test_dsr_monotonic_in_ic_strength(self):
        """IC 越强 DSR 越高 (多重检验校正下仍单调)."""
        weak = evaluate_factor(_ic_series(120, mean=0.005, sd=0.05), n_trials=10, **self.KW)
        strong = evaluate_factor(_ic_series(120, mean=0.030, sd=0.05), n_trials=10, **self.KW)
        assert strong["dsr"] > weak["dsr"]

    def test_more_trials_raises_bar(self):
        """M 越大 E[max_SR] 越高 → 同一序列 DSR 越低 (多重检验校正生效)."""
        ics = _ic_series(120, mean=0.01, sd=0.04)
        few = evaluate_factor(ics, n_trials=2, **self.KW)
        many = evaluate_factor(ics, n_trials=200, **self.KW)
        assert many["expected_max_sr"] > few["expected_max_sr"]
        assert many["dsr"] < few["dsr"]


class TestConfigWiring:
    def test_attribution_cpcv_keys_exist(self):
        from quant.config.loader import get
        assert get("attribution.dsr_degraded_threshold") == 0.5
        assert get("attribution.dsr_recover_threshold") == 0.95
        assert get("attribution.cpcv_min_days") == 40
        assert get("attribution.cpcv_lookback_days") == 120
        assert get("factor.evaluation.cpcv_groups") == 5
        assert get("factor.evaluation.embargo_days") == 5  # ADR-041: De Prado purged K-fold 标准

    def test_removed_legacy_keys_gone(self):
        """旧 L2 单切分比率阈值已下线 (test-v300)."""
        from quant.config.loader import get
        assert get("attribution.oos_warning_decay") is None
        assert get("attribution.oos_recovery_threshold") is None

    def test_default_thresholds_from_config(self):
        """不传阈值 → 读 config; 噪声序列仍 degraded."""
        v = evaluate_factor(_ic_series(120, mean=0.0, sd=0.05), n_trials=10)
        assert v["verdict"] == "degraded"
