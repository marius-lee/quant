# ADR 032: 资金分段与 Kelly 禁用边界 —— Nano/Micro/Small 三层架构

**状态**: 已决
**日期**: 2026-07-15
**作者**: Codex
**关联**: ADR 029, capital-segmentation-analysis-2026-07-15.md, HANDOFF.md#2026-07-12, HANDOFF.md#2026-07-15

## 背景

Kelly 预算法的适用性在项目中反复争论过至少三次：

1. **2026-07-12 之前**: `_equal_weight_greedy` 存在但从未被调用，所有层级统一走 `_kelly_greedy`
2. **2026-07-12 第一次** (HANDOFF): 动态阈值 `avg_price × LOT_SIZE × 2` 在 ¥5,000 资本下导致 0 仓位问题。结论：改回固定阈值，但 Kelly 仍用于贪婪层
3. **2026-07-15 第二次**: 用户质疑 "Kelly 对小资金太严了？只买 100 股？" 根因分析：Kelly 离散化误差在 ¥5,000 下 30-100%，产出的权重经整数舍入后只剩 1 手
4. **2026-07-15 第三次** (本文): 全面分析后决定：Kelly 仅在 Small 层 (≥¥100K) 启用

每次争论的核心是同一个问题：**整数手约束使 Kelly 在离散状态下失效**。但这个教训没有记录下来，导致反复踩坑。

## 决策

### 1. 资本分段采用三层固定阈值

| 层级 | 资金 | 最大持仓 | 算法 | 来源 |
|------|------|---------|------|------|
| Nano | < ¥30,000 | 1-3 只 | 贪心等权 (`_equal_weight_greedy`) | C1: P75 候选价 ¥2,678 × 3只 × 3手 ≈ ¥24K |
| Micro | ¥30,000 – 100,000 | 3-8 只 | 得分倾斜 (`_score_weighted_rounding`) | C2: 3-8只捕获 73-94% 分散化收益 |
| Small | ≥ ¥100,000 | 8-20 只 | Risk Parity → Kelly → MV | C4: Kelly 离散化误差 <10% 开始有价值 |

阈值从 config.yaml 读取 (`optimizer.nano_cap` / `optimizer.micro_cap`)，每季度用最新价格分布校验。

### 2. Kelly 禁用边界：< ¥100,000 不使用 Kelly

| 层级 | Kelly 是否启用 | 理由 |
|------|:---:|------|
| Nano | **禁止** | 离散化误差 30-100%，优化收益被噪声淹没 |
| Micro | **条件禁止** | ~25% 离散化误差。仅在 IC 稳定且 >5 只候选时可用，当前默认不启用 |
| Small | **启用** (半凯利) | <10% 离散化误差。kelly_fraction=4.0 (四分之一凯利) |

**禁止的代码表现**: `_tier()` 返回 "nano" 或 "micro" 时，`construct()` 不会调用 `_kelly_greedy()`。Kelly 仅在 Small 层作为 MV 的前置步骤被调用。

### 3. 阈值不动态化

2026-07-12 的动态阈值 (`avg_price × LOT_SIZE × 2`) 已验证会引入 0 仓位 bug。此后永久使用固定阈值，仅人工季度校验。

## 依据

完整推导见 [capital-segmentation-analysis-2026-07-15.md](../../docs/reports/capital-segmentation-analysis-2026-07-15.md)。核心约束：

- **C1 整手约束**: P75 候选价 ¥2,678/手。¥20,000 只能买 7 手，分给 8 只 → 必归零
- **C2 分散化边际递减**: ρ≈0.4, 3-5 只捕获 87% 分散化收益。20 只以上边际 <1%
- **C3 佣金效率**: 单笔 <¥10,000 时佣金吃掉 >100% alpha
- **C4 Kelly 离散化**: Kelly 假设连续赌注，整数手使 <¥50,000 下误差 >25%

文献支撑：
- Kelly (1956): 原始凯利假设连续押注
- Ralph Vince (1990): Fractional Kelly, 半凯利在已知胜率下最优
- MacLean, Thorp, Ziemba (2011): 四分之一凯利在胜率估计不确定时更安全
- DeMiguel, Garlappi, Uppal (2009): 1/N 在 N/T 比例高时优于 MV
- 华泰金工 (2020): A 股 <¥50 万建议 5-8 只

## 后果

### 正面
- Nano 层不再出现 "只买 1 手" 的尴尬 — 等权贪心确保资金集中在最强信号
- 摘除了 weighted→0 安全网 — ¥30,000 下加权分配不会归零
- 未来重构时 ADR 032 直接警示：**不要试图在 Nano/Micro 层引入 Kelly**

### 负面
- Micro 层 (¥30K-100K) 暂不使用 Kelly，放弃了这层资金的最优分配可能。但离散化误差说明这并非真正的损失

## 关联代码位置

| 文件 | 关键行/方法 | 作用 |
|------|-----------|------|
| `quant/config/config.yaml` | `optimizer.nano_cap: 30000`, `optimizer.micro_cap: 100000` | 层级阈值 |
| `quant/optimizer/portfolio.py` | `PortfolioConstructor._tier()` | 返回 nano/micro/small |
| `quant/optimizer/portfolio.py` | `PortfolioConstructor.construct()` | "nano"→`_equal_weight_greedy`, "small"→`_kelly_greedy` |
| `quant/optimizer/kelly.py` | `compute_lot_allocation()` | Kelly 仅在 Small 层被调用 |
| `docs/reports/capital-segmentation-analysis-2026-07-15.md` | — | 完整推导文档 |

## 反模式（禁止）

以下行为在本项目中永久禁止，违反即回归 bug：

1. **在 Nano/Micro 层的 construct 分支中调用 `_kelly_greedy()`**
2. **将阈值改回动态计算 (`avg_price × LOT_SIZE × N`)**
3. **在 `_kelly_greedy()` 或 `_equal_weight_greedy()` 返回 0 手时添加 fallback** — 这是隐藏问题，应该 throw
4. **在 `construct()` 的任何分支中吞掉 0 仓位错误** — 0 仓位是数据/参数问题，必须暴露
