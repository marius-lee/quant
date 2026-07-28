# AI 因子自动化搜索 — 可行性分析

**日期**: 2026-07-28
**状态**: 分析中

---

## 背景

幻方量化已部署 AI 因子工厂，通过遗传规划自动生成并筛选因子。评估本项目是否具备同类能力的基础。

## 幻方做法

```
搜索空间: {price, volume, return, amount} × {+, -, *, /, rank, ts_mean, ts_std, delay, corr, ...}
搜索算法: 遗传规划 (crossover, mutation, selection)
适应度:   Rank IC × (1 - |自相关|) × 去冗余惩罚
校验:     Walk-forward + PBO + CPCV + 多重检验校正
```

## 现有基础 (80%)

| 组件 | 现状 |
|---|---|
| 52 预计算原语 (log_ret, vol_N, roll_max_N 等) | ✅ |
| 因子计算引擎 compute_all_factors + shortcut | ✅ |
| 适应度评估 Phase 2 单因子检验 + CPCV+DSR | ✅ |
| 多重检验校正 DSR + MinTRL | ✅ |
| 去冗余 拥挤度检测 (G2) + 同号高相关 (G5) | ✅ |
| PIT 校验 因子缓存 + trailing slice | ✅ |
| 符号回归引擎 gplearn | ❌ 缺失 |
| 遗传搜索循环 | ❌ 缺失 |
| 表达式存储/版本管理 | ❌ 缺失 |

## 推荐方案

**gplearn** 符号回归 + 现有的 52 原语 + 8 阶段评估管线。

### Phase 1: 表达式→因子编译器
表达式字符串解析 → 可执行因子函数。在现有原语和 shortcut 映射上只需加解析层。

### Phase 2: 遗传搜索循环
每代 500 表达式 × 20 代，用预计算原语复用，每因子 ~0.5s。

### Phase 3: 自动注册
通过验证的因子 → factor_registry (candidate) → 接入评估管线。

## 风险

| 风险 | 缓解 |
|---|---|
| 过拟合 | 已有 CPCV+DSR 校验链 |
| 计算量 | 预计算原语复用，批量评估 |
| 数据挖掘偏误 | Walk-forward OOS 独立于搜索窗口 |
