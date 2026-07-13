"""Gap 3: Market regime detection — 3-state HMM on CSI 300 returns.

Regimes:
  0: Bull — positive drift, moderate volatility
  1: Bear — negative drift, high volatility
  2: Sideways — near-zero drift, low volatility

Online inference: forward filter only (no look-ahead Viterbi).
Regime-conditional factor IC weights stored in factor_regime_stats table.
"""

import os, sqlite3, json
import numpy as np
import pandas as pd
from utils.logger import get_logger

_log = get_logger("regime.detector")

_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")

REGIME_LABELS = {0: "bull", 1: "bear", 2: "sideways"}
FACTOR_REGIME_BIAS = {
    # Factor names that perform BETTER in each regime
    "bull": ["momentum", "gap", "limit_up", "residual_momentum", "max_ret",
             "seal_turnover_ratio", "seal_time", "zt_streak"],
    "bear": ["volatility", "downside_volatility", "idio_vol", "amihud",
             "size", "debt_ratio", "high52w_dist"],
    "sideways": ["reversal", "turnover_rev", "rsi_rev", "skewness",
                 "ma_alignment", "ideal_amplitude"],
}


class RegimeDetector:
    """3-state HMM regime detector using rolling CSI 300 returns."""

    def __init__(self):
        self._model = None
        self._last_state_prob = None
        self._train_window = 252

    def _features(self, benchmark_returns, window=20):
        """Compute features: rolling mean return and rolling volatility."""
        if len(benchmark_returns) < window + 10:
            return np.zeros((1, 2))
        roll_mean = benchmark_returns.rolling(window=window).mean().dropna()
        roll_std = benchmark_returns.rolling(window=window).std().dropna()
        common = roll_mean.index.intersection(roll_std.index)
        if len(common) < 20:
            return np.zeros((1, 2))
        X = np.column_stack([roll_mean.loc[common].values, roll_std.loc[common].values])
        return X

    def train(self, benchmark_returns, n_states=3):
        """Train HMM on historical CSI 300 returns. Returns self."""
        X = self._features(benchmark_returns)
        if X.shape[0] < 50:
            _log.warning(f"regime train: only {X.shape[0]} samples — insufficient")
            return self

        try:
            from hmmlearn import hmm
            model = hmm.GaussianHMM(
                n_components=n_states,
                covariance_type="full",
                n_iter=200,
                random_state=42,
            )
            model.fit(X)
            self._model = model

            # Order states by mean: state 0 = highest drift (bull)
            means = model.means_[:, 0]
            order = np.argsort(means)[::-1]
            self._model.means_ = model.means_[order]
            self._model.covars_ = model.covars_[order]
            self._model.startprob_ = model.startprob_[order]
            self._model.transmat_ = model.transmat_[order][:, order]

            _log.info(f"regime HMM trained: means={model.means_[:, 0].round(5)} "
                      f"n_samples={X.shape[0]}")
        except ImportError:
            _log.warning("hmmlearn not installed — regime detection disabled")

        return self

    def predict_proba(self, benchmark_returns):
        """Online forward-filter: don't use Viterbi (no look-ahead).

        Returns: (regime_label, probability_dict)
        """
        if self._model is None:
            return ("unknown", {})

        X = self._features(benchmark_returns)
        if X.shape[0] < 10:
            return ("unknown", {})

        try:
            # Use the last observation only
            last_obs = X[-1:].reshape(1, -1)
            # Compute forward probabilities (filtering, not smoothing)
            logprob, posteriors = self._model.score_samples(X[-60:])
            if posteriors.shape[0] == 0:
                return ("unknown", {})
            probs = posteriors[-1]
            regime_idx = int(np.argmax(probs))
            prob_dict = {REGIME_LABELS[i]: float(p) for i, p in enumerate(probs)}
            return (REGIME_LABELS[regime_idx], prob_dict)
        except Exception as e:
            _log.warning(f"predict_proba: {e}")
            return ("unknown", {})


# ── Module-level singleton ──
_detector = None


def get_current_regime():
    """Get current market regime using CSI 300 data. Called daily."""
    global _detector

    try:
        from data.benchmark import get_benchmark_returns
        returns = get_benchmark_returns("000300", start="2024-01-01")

        if _detector is None:
            _detector = RegimeDetector()
            _detector.train(returns)

        return _detector.predict_proba(returns)
    except Exception as e:
        _log.warning(f"get_current_regime: {e}")
        return ("unknown", {})


def get_regime_weights(factor_names, ic_map, regime_label, regime_probs):
    """Compute regime-conditional factor weights.

    Boosts factors known to perform well in the current regime.
    For factors not matching any regime bias, IC weights are unchanged.

    Returns: dict[factor_name] = weight
    """
    if regime_label not in FACTOR_REGIME_BIAS or not regime_probs:
        return ic_map or {}

    bias_set = set(FACTOR_REGIME_BIAS.get(regime_label, []))
    confidence = regime_probs.get(regime_label, 0.5)
    boost = 1.0 + confidence * 0.3  # up to 30% boost

    weights = dict(ic_map) if ic_map else {}
    for name in factor_names:
        if name not in weights:
            weights[name] = 1.0 / max(len(factor_names), 1)

        # Check if this factor benefits from current regime
        for keyword in bias_set:
            if keyword in name:
                weights[name] *= boost
                break

    # Renormalize
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total * len(weights) for k, v in weights.items()}

    return weights
