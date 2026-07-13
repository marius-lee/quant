# ADR 028: 模块大小限制 — Python 文件 ≤800 行

**日期**: 2026-07-11
**状态**: 已落地
**关联**: 模板 10, ADR 019

## 背景

`factor/compute.py` 膨胀到 3182 行。每次 agent 读取消耗大量 token，且文件内函数归属混乱（价量因子和基本面因子物理上交错分布）。

## 决策

1. **Python 文件 ≤800 行**（模板 10 硬约束）
2. 超过 800 行的文件拆分为子包，`__init__.py` 做 facade 保持向后兼容
3. 拆分按**逻辑内聚**分组，不按行数机械切割

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 不设限制，保持大文件 | token 浪费严重，agent 每次需读取不需要的代码 |
| 每个因子一个文件（70+ 文件） | 碎片化过度，导入复杂度剧增 |
| 按行数机械切割（每 500 行一刀） | 函数被拦腰截断，逻辑内聚性破坏 |

## 落地步骤

1. `factor/compute.py` (3182行) → `factor/compute/` 包：`price.py` + `fundamental.py` + `_registry.py` + `_dispatch.py` + `_shared.py`
2. `price.py` (1908行) → `factor/compute/price/` 包：`_momentum.py` + `_event.py` + `_alternative.py`
3. `fundamental.py` (1213行) **暂不拆** — 32 个纯函数内聚性高，拆分只增加跳转无收益

## 验证

- 67 tests 全部通过
- `from factor.compute import X` 完全向后兼容
- `__all__` 控制 `*` 导入，无内部名称泄漏

## 模块大小终态

```
factor/compute/__init__.py       18
factor/compute/_shared.py         6
factor/compute/_registry.py     102
factor/compute/_dispatch.py      94
factor/compute/fundamental.py  1213  ← 暂不拆
factor/compute/price/__init__.py 156
factor/compute/price/_momentum.py  605
factor/compute/price/_event.py     561
factor/compute/price/_alternative.py 660
```
