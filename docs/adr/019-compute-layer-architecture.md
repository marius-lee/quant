# ADR 019: 因子计算层架构修复 — 分离 production filter 与 evaluation scope

**日期**: 2026-07-05
**状态**: 已落地 (P45, commit 17a1377, 2026-07-06)
**关联**: ADR 018 (因子评估死锁), Grinold & Kahn Ch.7-8

## 架构问题

ADR 018 识别了死锁症状：deprecated 因子永不可恢复。根因是架构层面的职责混淆：

```
load_active_price_factors()     ← 计算层函数
    WHERE status='active'       ← 内含策略决策 (production filter)
```

Grinold & Kahn 框架下，Alpha Generation（Ch.7，因子计算）和 Alpha Combination（Ch.8，因子选择）应是两个独立阶段。当前代码将它们合并到了一个函数里。

## 影响范围 (6 处调用点)

| 调用点 | 文件 | 用途 | 修改 |
|--------|------|------|------|
| `compute_all_factors` | `pipeline.py:200` | 生产管道 | **不改**（默认 active） |
| `compute_factor_stats` | `eval_stepwise.sh:24` | 评估管道 | **传全量名** |
| `get_factor_names` | `stats_cache.py:87` | IC 统计 | **透传参数** |
| `get_factor_names` | `stats_cache.py:292` | IC 缓存 | **透传参数** |
| `compute_factor_stats` | `stats_cache.py:334` | 快照 | **不改**（默认 active） |
| `load_active_*_factors` | `validate.py:87-88` | 校验 | **不改**（校验 active 一致性） |

无测试覆盖这些入口函数，修改不会破坏现有测试。

## 修改方案

### 1. `factor/compute.py` — 3 个函数增加可选参数

```python
# 旧
def load_active_price_factors():
    ... WHERE status='active' ...

# 新
def load_active_price_factors(status_filter='active'):
    ... WHERE (status_filter is passed through or omitted) ...

# 同样: load_active_fundamental_factors, get_factor_names
```

```python
# 旧
def compute_all_factors(data, date, fundamentals=None, benchmark_ret=None):

# 新
def compute_all_factors(data, date, fundamentals=None, benchmark_ret=None, factor_names=None):
    if factor_names is not None:
        # 显式列表: 从 _PRICE_FN_MAP / _FUNDAMENTAL_FN_MAP 交叉查找
        price_factors = {n: _PRICE_FN_MAP[n] for n in factor_names if n in _PRICE_FN_MAP}
        fund_factors = {n: _FUNDAMENTAL_FN_MAP[n] for n in factor_names if n in _FUNDAMENTAL_FN_MAP}
    else:
        # 默认: 读 registry status='active'
        price_factors = load_active_price_factors()
        fund_factors = load_active_fundamental_factors()
```

### 2. `factor/stats_cache.py` — `compute_factor_stats` 增加 factor_names 参数

```python
def compute_factor_stats(n_symbols=800, lookback=120, factor_names=None):
    if factor_names is not None:
        names = factor_names
    else:
        names = get_factor_names()  # 原有行为: active only
    ...
    fv = compute_all_factors(data, date_str, factor_names=names, ...)
```

### 3. `eval_stepwise.sh` — 传全量因子名

```python
# 在调用 compute_factor_stats 之前
import sqlite3
conn = sqlite3.connect('data/market.db')
all_names = [r[0] for r in conn.execute('SELECT name FROM factor_registry')]
conn.close()

stats = compute_factor_stats(..., factor_names=all_names)
```

## 向后兼容

- 所有新增参数默认值保持当前行为
- `pipeline.py` 不改，生产管道继续只用 active
- `validate.py` 不改，校验逻辑不变
- `load_ic_map_from_cache` 改 `get_factor_names(None)` → 加载全量 IC 缓存

## 与 ADR 018 的关系

ADR 018 描述症状和死锁机制。ADR 019 是根治方案：去掉计算层的策略判断，让调用方决定计算范围。修复后 ADR 018 的死锁自动解除。
