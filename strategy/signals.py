"""交易信号生成。

将模型预测的预期收益转化为具体的买卖信号:
  - 分位数法: 买 Top N%, 卖 Bottom N%
  - 阈值法: 预期收益 > 阈值则买入
"""

import numpy as np
import pandas as pd


def generate_signals(predictions: pd.Series, top_pct: float = 0.2) -> pd.Series:
    """多头信号：买预测得分最高的 Top N% 股票。返回 1=买入, 0=持有。"""
    signals = pd.Series(0, index=predictions.index)
    n_buy = max(1, int(len(predictions) * top_pct))
    signals[predictions.nlargest(n_buy).index] = 1
    return signals


def generate_weights(signals: pd.Series,
                     predictions: pd.Series = None,
                     method: str = "equal") -> pd.Series:
    """将信号转为仓位权重。

    method:
      - equal: 等权
      - prediction: 按预测值加权
      - risk_parity: 等风险贡献（简化: 波动率倒数加权）
    """
    long_mask = signals > 0
    weights = pd.Series(0.0, index=signals.index)

    if long_mask.sum() == 0:
        return weights

    if method == "equal":
        weights[long_mask] = 1.0 / long_mask.sum()

    elif method == "prediction" and predictions is not None:
        pos_pred = predictions[long_mask].clip(lower=0)
        total = pos_pred.sum()
        if total > 0:
            weights[long_mask] = pos_pred / total
        else:
            # 全部预测为负或零时回退为等权
            weights[long_mask] = 1.0 / long_mask.sum()

    elif method == "risk_parity":
        raise NotImplementedError(
            "risk_parity method is not yet implemented. "
            "Use 'equal' or 'prediction' instead."
        )

    else:
        raise ValueError(f"unknown weight method: {method}. Use 'equal', 'prediction', or 'risk_parity' (NYI).")

    return weights


if __name__ == "__main__":
    pred = pd.Series(
        np.random.randn(10) * 0.02,
        index=stocks
    )
    print("Predictions:")
    print(pred.sort_values(ascending=False))
    sig = generate_signals(pred)
    w = generate_weights(sig, pred, method="prediction")
    print("\nWeights:")
    print(w[w > 0])
