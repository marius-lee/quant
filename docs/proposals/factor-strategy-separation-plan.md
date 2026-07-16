# 因子回测与策略回测分离：修改方案 v2

> 修订: 2026-07-16 | 基于 v1 讨论后的精炼版本
> 状态: 已实施 Phase A

---

## 核心架构：三个独立操作，共享数据底座

```
                        ┌──→ 因子回测（评估因子质量）
factor_cache.db ←──────┤     独立的, 不依赖策略回测结果
                        │     输入: 因子值 → 输出: 因子报告 + factor_registry
                        │
                        └──→ 策略回测（模拟交易执行）
                              独立的, 不依赖因子回测结果
                              输入: 因子值 → 输出: 权益曲线 + trades
```

**两个回测互不依赖对方的结果，只依赖共享数据**（`factor_cache.db`）。

对标: Quantopian 的双模式设计（Pipeline API 在研究模式和算法模式独立运行）。

---

## 已实施部分 (test-v111)

### 1. FactorStore — 因子值物化层 (`quant/factor/store.py`)

提供三个核心方法:

| 方法 | 作用 | 调用方 |
|------|------|--------|
| `materialize(dates, factors, symbols)` | 批量计算因子值 → 写入 `factor_cache.db` | CLI / 调度器 |
| `load(date, symbols, factors)` | 读取某日因子值 → `{name: Series}` | `generate_signals()` |
| `is_materialized(dates, factors)` | 检查覆盖率 | 自动检测 |

存储: `quant/data/factor_cache.db`（独立于 market.db）  
表: `factor_values(date, symbol, factor, raw_value, zscore)` + `materialization_log`

### 2. pipeline.py — generate_signals() 加 factor_store 参数

```python
def generate_signals(..., factor_store=None):
    if factor_store is not None:
        factor_values = factor_store.load(date, symbols)  # 读缓存
    if not factor_values:
        factor_values = compute_all_factors(...)           # 实时计算（向后兼容）
```

### 3. loop.py — run_backtest() 加 factor_store 参数

```python
def run_backtest(..., factor_store=None):
    # factor_store 透传给 generate_signals()
    kwargs["factor_store"] = factor_store
```

### 4. materialize_factors.py — CLI 工具

```bash
PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py
PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py 2026-01-01 2026-07-15
PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py --force
PYTHONPATH=. .venv/bin/python scripts/materialize_factors.py --status
```

---

## 待实施部分 (Phase B/C)

### Phase B: 冒烟测试 + 回测接入

smoke_test.py 在调用 run_backtest() 前自动物化:

```python
fs = FactorStore()
fs.materialize(date_range, factor_names, symbols, store=store)
run_backtest(..., factor_store=fs)
```

### Phase C: Web 界面

两个独立按钮: "因子回测" / "策略回测"，各自触发独立 API 端点。

---

## 与业界标准的对照

| 报告中的最佳实践 | 本方案对应 |
|-----------------|-----------|
| 信号表中介 (VNPY parquet模式) | factor_cache.db (SQLite) |
| 因子与策略分离 (Quantopian双模式) | 两个独立操作, 共享 factor_cache.db |
| 策略调参不触发因子重算 | materialize一次, run_backtest N次 |
| 因子评估独立于策略执行 | 因子回测和策略回测互不依赖 |
| 双引擎: 向量化因子 + 事件驱动策略 | materialize.py向量化 + loop.py事件驱动 |

---

## 不变模块

`factor/` (57个因子函数), `ic.py`, `alpha/`, `risk/`, `optimizer/`, `execution/`, `evaluation/` (Phase 1-7), `scheduler/`, `pipeline.py` (向后兼容路径保留) — 全部不变。
