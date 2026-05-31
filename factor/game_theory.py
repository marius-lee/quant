"""博弈论/微观结构因子 — 7类 × 4窗口 = 28因子"""
import numpy as np
import pandas as pd
from factor.base import BaseFactor


class GameTheoryFactors(BaseFactor):
    def __init__(self, na_fill="median", windows=(5, 10, 20, 60)):
        super().__init__(na_fill)
        self.windows = windows

    def compute(self, data: dict) -> pd.DataFrame:
        close = data["close"]
        ret = close.pct_change()
        amount = data.get("amount", close * data.get("volume", close * 1e7))
        dollar_vol = (amount / 1e8).replace(0, np.nan)
        H = data.get("high", close * 1.01)
        L = data.get("low", close * 0.99)
        V = data.get("volume", close * 1e7)
        # PIN proxy 需要开盘价（隔夜波动率 / 日内波动率）
        open_price = data.get("open", close.shift(1))

        il = ret.abs() / dollar_vol
        hl = (H - L) / close
        kyle = ret.abs() / np.sqrt(V.replace(0, np.nan))
        mkt = ret.mean(axis=1)
        dev = ret.sub(mkt, axis=0).abs()

        all_vals, factor_names = [], []
        for w in self.windows:
            ov_vol = (open_price / close.shift(1) - 1).rolling(w).std()
            tv_vol = ret.rolling(w).std()
            pairs = [
                (f"amihud_il_{w}d", il.rolling(w).mean()),
                (f"pin_proxy_{w}d", (ov_vol / tv_vol.replace(0, np.nan)).replace(np.inf, 0)),
                (f"herding_csmad_{w}d", dev.rolling(w).mean()),
                (f"hl_spread_{w}d", hl.rolling(w).mean()),
                (f"kyle_lambda_{w}d", kyle.rolling(w).mean()),
                (f"info_arrival_{w}d", (V / V.rolling(w*5, min_periods=1).mean()).clip(0, 10)),
                (f"nash_distortion_{w}d", ret.rolling(w).skew().abs() + (ret.rolling(w).kurt() - 3).abs()),
            ]
            for fname, vals in pairs:
                factor_names.append(fname)
                all_vals.append(vals)

        stocks = close.columns.tolist()
        all_cols = [(fn, s) for fn in factor_names for s in stocks]
        result = pd.concat(all_vals, axis=1)
        result.columns = pd.MultiIndex.from_tuples(all_cols)
        return self.process(result)
