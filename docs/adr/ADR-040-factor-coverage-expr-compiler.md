# ADR-040: 因子覆盖补充 + AI 表达式编译器

**日期**: 2026-07-28
**状态**: ✅ 已实施

---

## 背景

审计发现 4 类因子薄弱、10 类缺失。优先补齐已有数据的缺失因子。

## 新增因子 (5 个, candidate 状态)

| 因子 | 类别 | 来源 |
|---|---|---|
| `amihud_20d` | 流动性 | Amihud (2002) 短期窗口 |
| `turnover_adj_amihud_20d` | 流动性 | Amihud / sqrt(turnover) 去量价异象 |
| `lhb_intensity_5d` | 龙虎榜 | net_buy/circ_mv 主力资金强度 |
| `lhb_reversal_5d` | 龙虎榜 | -post_5d 上榜后反转 |
| `lhb_freq_60d` | 龙虎榜 | log(1+count) 上榜频率 |

## AI 表达式编译器

**文件**: `quant/factor/compute/expr_compiler.py` (~300 行)

递归下降解析器，文本表达式 → 可执行因子函数：

```
"ts_mean(rank(close/open), 20) / ts_std(volume, 60)"
    → Parser → AST → evaluate(data, date) → pd.Series
```

支持: 算术(+,-,*,/,^), 一元(abs,sqrt,log,neg,sign,sqr),
     时序(ts_mean/std/max/min/sum/delay/delta(N)),
     截面(rank,zscore,cs_mean,cs_std),
     字段引用(close,open,high,low,volume,amount,turnover)

与现有因子函数签名完全兼容: `(data, date) → Series`

## 变更清单

| 文件 | 变更 |
|---|---|
| `quant/factor/compute/price/_momentum.py` | +amihud_20d, +turnover_adj_amihud_20d |
| `quant/factor/compute/price/_event.py` | +lhb_intensity_5d, +lhb_reversal_5d, +lhb_freq_60d |
| `quant/factor/compute/price/__init__.py` | 注册 5 新因子 + 导入 |
| `quant/factor/compute/expr_compiler.py` | NEW: AI 表达式编译器 |
| `factor_registry` (market.db) | 5 新因子注册 (candidate) |

## 测试

- 221 测试全部通过 ✅
- 表达式编译器 6 个表达式类型验证通过 ✅
