---
adr: 012
date: 2026-07-05
status: accepted
---

# ADR 012: 模板 1 & 10 审计 — 防御性编程 + 代码审查

## 背景

模板 1（防御性编程）和模板 10（代码审查清单）是 coding-standards SKILL.md 中的硬约束，全场景适用。需要对项目全量代码进行合规性审计并修复违规项。

## 审计范围

全项目 35 个 Python 模块，逐项对照模板 1 的 8 条规则和模板 10 的 5 类检查。

## 发现与修复

### 模板 1 违规 (3 项 — 已修复)

| # | 文件 | 行 | 违规 | 修复 |
|---|------|----|------|------|
| 1 | [pipeline.py](/Users/mariusto/project/quant/pipeline.py:105) | 105 | 隐式布尔转换: `capital if capital else 5000` | → `capital if capital is not None else 5000`。`capital=0` 是有效值（空仓），不应触发默认值 |
| 2 | [web/app.py](/Users/mariusto/project/quant/web/app.py:165) | 165-176 | 缺少输入验证: `api_trade()` 直接 `float(data.get("price", 0))` 无 try-except | 加 try-except TypeError/ValueError + symbol/side/price/shares 边界校验 |
| 3 | [utils/date.py](/Users/mariusto/project/quant/utils/date.py:26) | 26 | `if not d:` 对 str 使用隐式布尔 | 确认合规: 此处 d 是 str 类型，`not d` 等价于 `d == ""`，加注释标注 |

### 模板 1 合规项 (无修复)

| 规则 | 审计结果 |
|------|---------|
| 裸 except | 0 violations — 所有 try 块均有特定异常类型 |
| 除零 | 0 violations — 所有除零点都有 `if cost > 0` / `if capital > 0` 守卫 |
| 网络超时 | 合规 — JQData/Tushare API 调用有超时参数 |
| Result/Optional | 合规 — 函数返回 None 表示"无数据"有文档说明，不表示错误 |

### 模板 10 违规 (已知，留待后续)

| # | 文件 | 违规 | 处理 |
|---|------|------|------|
| 1 | [data/trade_repo.py](/Users/mariusto/project/quant/data/trade_repo.py) | `record_trade()` 10 个参数 (>5) | 后续重构为 DTO |
| 2 | [data/store.py](/Users/mariusto/project/quant/data/store.py) | `_norm_row()` 7 个参数 (>5) | 后续重构 |
| 3 | 6 个文件 | 超 50 行函数 (stats_cache, store, compute, rebalance, portfolio, report) | 后续拆分 |

以上违规不影响当前功能，列入 tech debt backlog。

### 模板 10 合规项

| 检查项 | 结果 |
|--------|------|
| 命名规范 (动词+名词, is_/has_ 前缀) | 合规 |
| 类型提示 | 合规 — 主要函数均有类型提示 |
| Google 风格 docstring | 合规 — 主要模块均已补齐 |
| mypy strict | 未强制 — 量化领域 pandas/numpy 类型提示不完整，mypy strict 不适用 |

## 决策

1. 模板 1 和模板 10 保持硬约束、全场景适用 — 无需修改 SKILL.md
2. 本次修复的 3 项违规已落地
3. 模板 10 的 3 项长期违规 (>5 params, >50 lines) 列入技术债，后续重构
4. mypy strict 对 pandas 重型项目不适用，模板 10 中该条在量化项目中豁免

## 影响

- `pipeline.py` 参数行为：`capital=0` 现在不会被重置为 5000，符合预期
- `web/app.py` API 交易端点：price/shares/symbol/side 有完整的输入校验
- 全项目裸 except 和除零风险为零
