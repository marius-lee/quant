# 流动性过滤分析

日期: 2026-06-05

## 核心结论

日成交额与股票质量**无关**。A股实证: 低换手率股票未来收益**高于**高换手率股票 (低流动性溢价, Amihud 2002)。

流动性过滤是**执行可行性过滤**, 不是质量过滤。

## 当前状态

`data/repository.py` → `get_qualified(capital=...)`

```
min_daily_amount = (capital / max_positions) × 10

¥5,000:  最低 ¥16,667/天
¥20,000: 最低 ¥66,667/天
¥100万:  最低 ¥500,000/天
```

## 其他量化软件的做法

| 平台/机构 | 做法 | 逻辑 |
|------|------|------|
| 海通证券金工 | 成交额×5% ≥ 组合规模×0.1% | 按资金规模动态算 |
| QuantConnect | dollar_volume > $10M | 机构级绝对阈值 |
| Goyenko et al. (2024 NBER) | 预测成交量 + Almgren-Chriss 冲击模型 | 最先进 |
| WorldQuant | TOP3000 流动性池 | 固定池大小 |

## A 股实际数据 (2026-06-03)

- 37.6% 股票日成交额 < ¥100,000
- 中位数 ¥158,195
- 最低 ¥3,221

## 来源

- Amihud (2002): Illiquidity and stock returns — 低流动性溢价奠基论文
- Portfolio123 社区标准: 持仓 ≤ 日均成交额 10%
- 海通证券: 选股因子系列研究(九十一), 2023
- Goyenko, Kelly, Moskowitz, Su & Zhang: Trading Volume Alpha, NBER 2024
