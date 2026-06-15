# 训练/测试分割比例

日期: 2026-06-05

## 原值

`config.yaml`: `train_split: 0.92` — 随手写的, 无来源。

## 搜索结论

| 来源 | 推荐 |
|------|:--:|
| Quant StackExchange (2024) | 至少 30% OOS, 80/20 是标准起点 |
| MLJAR Walk-Forward Guide (2025) | 至少 30 个 OOS fold |
| Krynska (2023) | 延长训练、缩短 OOS 可改善稳定性 |
| LSTM 股票预测 (Supri et al., 2023) | 80/20 最优, 70/30 MAPE 最稳定 |

## 修改

`train_split: 0.70` — 基于"至少 30% OOS"行业共识。

```
1311 天 × 0.70:
  训练: 917 天 (~3.7 年)
  测试: 394 天 (~1.6 年)
```

## 实测对比

| 指标 | 92/8 (105d测试) | 70/30 (467d测试) |
|------|:--:|:--:|
| Sharpe | 1.20 | **-0.30** |
| 年化 | +95.8% | **-13.9%** |

92/8 的短暂窗口产生虚假信心。70/30 暴露真实表现。

## 来源

- Quant StackExchange: Train-test split on time series data (2024)
- Supri et al.: Asian Stock Index Prediction Using Split Data (2023)
