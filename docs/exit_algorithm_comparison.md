# 退出算法对比

日期: 2026-06-05

## 当前系统

移动止损 + ATR 动态距离 + 分批止盈

- 硬止损: ATR × 2 (情绪自适应: 冰点 -2% ~ 高潮 -5%)
- 移动止损激活: ATR × 1
- 移动止损距离: ATR × 2
- 分批止盈: ATR × 4 触发, 建议减仓 50%
- 持有上限: 5 个交易日

## 业界算法对比

| 算法 | 原理 | 适用场景 | 我们实现 |
|------|------|------|:--:|
| **ATR 止损** | 止损距离 = N × ATR(14) | 波动自适应 | ✅ 已实现 |
| **Chandelier Exit** | 从最高点回落 N × ATR(3) | 趋势跟踪 | ⚠️ 近似 (用 ATR×2) |
| **Parabolic SAR** | 抛物线加速跟踪 | 趋势加速 | ❌ |
| **Supertrend** | ATR × 乘数 + 中轨 | 简单有效 | ❌ |
| **EGARCH 波动预测** | 预测下一期波动率 | 前向预测 | ❌ |
| **马尔可夫区制切换** | 高/低波动区制不同参数 | 区制自适应 | ❌ |
| **DQN 强化学习** | RL Agent 学习退出 | 理论最优 | ❌ (不稳定) |

## 实际效果

固定止盈被移除 (截断妖股主升浪)。移动止损替代固定止盈。

## 来源

- ATR: J. Welles Wilder, New Concepts in Technical Trading Systems (1978)
- Chandelier Exit: Le Beau & Lucas, Technical Traders Guide (1992)
- 止损 -5%: 大连理工《止损策略能否削弱情绪对收益的非理性影响》(2020)
- 止盈 +8%: 上交大MBA《证券交易短线操作技巧研究》
- arXiv: Leung et al. — trailing stop activation 1-5% for swing trading
