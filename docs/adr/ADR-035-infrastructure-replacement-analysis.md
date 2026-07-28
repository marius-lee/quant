# ADR-035: 基础设施替换分析报告

**日期**: 2026-07-28  
**状态**: 已评审  
**决策**: 渐进替换 — vnpy 优先，Qlib 其次，NautilusTrader 暂缓

---

## 背景

收到架构建议：三个自研模块可被业界成熟项目替代：

| 自研模块 | 建议替换 | 声称优势 |
|---|---|---|
| `backtest/` 回测引擎 | NautilusTrader | Rust纳秒级事件驱动，回测=实盘零代码改动，速度100x+ |
| `execution/` 实盘执行 | vnpy 4.4 | 已对接国内20+券商(CTP/XTP/奇点/TORA) |
| `factor/` + `alpha/` 因子体系 | Qlib | 微软出品，Alpha158/360 + 20种ML模型 |

## 代码审计

已通读全部核心模块（总计约7500行）：

| 模块 | 文件数 | 行数 |
|---|---|---|
| `backtest/` | 5 | ~878 |
| `execution/` | 8 | ~1,549 |
| `factor/compute/` | 11 | ~2,220 |
| `alpha/` | 4 | ~587 |
| `optimizer/` | 5 | ~1,371 |
| `risk/` | 5 | ~937 |

## 一、NautilusTrader → 回测引擎

### 当前回测架构分析

回测主循环（`backtest/loop.py`）性能特征：

```
因子计算 (每天57因子×N股票):  ~80% 时间
信号生成 (alpha合成+优化器):   ~15% 时间
执行循环 (订单匹配+成本计算):  ~3% 时间  ← NautilusTrader 只能加速这部分
诊断分析 (回测后):             ~2% 时间
```

已有优化：
- 预加载全量数据（消除843次DB查询）
- 预计算共享算子（52个中间量）
- 因子缓存物化（`factor_store`）
- 回测热路径冷却用内存dict（零DB写）

### 不可替换的深度定制逻辑

| 能力 | 位置 | 说明 |
|---|---|---|
| 因子计算+Alpha合成 | `pipeline.generate_signals()` | 核心IP，NautilusTrader完全不涉及 |
| Walk-forward IC重训 | `loop.py` L234-242 | 每N天重算IC权重 |
| Point-in-time regime HMM | `loop.py` L196-206 | 防前视的前向滤波 |
| 逐日FactorTracker | `analyze.py` | 因子贡献归因 |
| A股板块涨跌停 | `engine.py` `_price_limit_pct()` | 主板±10%，创业板±20%，北交所±30% |
| 除权除息检测 | `engine.py` `_check_ex_dividend()` | 跳变检测→跳过买入 |
| T+1约束 | `engine.py` via `repo.check_t1()` | 今日买入次日才能卖 |
| 冷却注册表 | `stop_loss.py` `RiskManager` | 止损后N天禁止买回 |
| Grinold §8.3成本带 | `portfolio.py` `_apply_tc_band()` | 换仓收益<λ×成本时保留原持仓 |

### 结论：❌ 不建议替换

NautilusTrader适合从零搭建的场景，但你已有成熟的回测管线。替换代价极高（A股特化逻辑需全部重新实现），收益极低（瓶颈在因子计算而非事件循环）。

---

## 二、vnpy → 实盘执行

### 当前"实盘"路径审计

`LiveExecutionModel` 的 `execute_sells()` 和 `execute_buys()` 最终都调用 `ctx.engine.execute()`：

```python
class LiveExecutionModel(ExecutionModel):
    def execute_sells(self, orders, ctx):
        ctx.engine.execute(orders, ...)  # ← SQLite模拟写入！！！
    
    def execute_buys(self, orders, ctx):
        # 熔断检查 → OrderManager挂限价单 → 最终仍走 engine.execute()
        # ← SQLite模拟写入！！！
```

**关键发现：当前没有任何真实券商对接。所有"实盘"执行都是模拟。**

### 已实现但无实际作用的组件

| 组件 | 状态 |
|---|---|
| `OrderManager` 限价单管理 | ✅ 完整实现，但最终走模拟成交 |
| `LiveExecutionModel` 熔断检查 | ✅ DB持久化熔断标志 |
| 涨停封死检测+重分配 | ✅ 开盘时检测+重分配 |
| 盘中ATR止盈止损 | ✅ monitor每30s扫描 |
| 尾盘强制补单 | ✅ 14:55强制市价成交未成交单 |

### 结论：✅ 强烈建议替换执行层底部

vnpy应替换的是执行链的最底层——把`engine.execute()`从SQLite模拟写入替换为真实券商下单。

**保留 → 替换对照**：

```
保留 (核心IP)                          替换底层 (vnpy)
─────────────────                      ────────────────
ExecutionModel 共用链                   execute_sells/buys 的实现
  ├─ 冷却过滤 (RiskManager)             从 engine.execute() 模拟
  ├─ 硬止损 (check_hard_stop)           → vnpy CTP/XTP 真实下单
  ├─ delta 计算 (compute_trades)       
  ├─ validate + alpha 裁剪             
  └─ 分单成交 ← 换 vnpy               

CostModel (自有成本模型)                vnpy 不替换，继续用
RiskManager (ATR止盈止损+冷却)          vnpy 不替换
交易日历 (calendar.py)                  保留（比 vnpy 更灵活）
报价 (quote.py)                          可用 vnpy 行情替代腾讯/新浪
```

---

## 三、Qlib → 因子体系

### 当前因子架构

```
57个因子 (41价格 + 16基本面)
  ├─ 因子计算: 预计算52个中间量 → shortcut映射 → 零fallback
  ├─ 状态管理: active / monitoring / candidate / retired / rejected 完整生命周期
  ├─ 评估管线: Phase 1-8 (数据→单因子→OOS→成本→监控→回测→Walk-forward→实盘一致性)
  └─ Alpha合成: sleeve / composite / IC加权 / 等权 / 交集

特色能力:
  ├─ 行业轮动 (美林时钟)
  ├─ 多周期确认 (周线+日线)
  ├─ HMM regime检测 → 条件因子调权
  ├─ Bayesian IC shrinkage
  └─ Capital-adaptive 组合优化 (Nano/Micro/Small三层)
```

### Qlib可补充的弱项

| 弱项 | Qlib方案 | 价值 |
|---|---|---|
| 线性IC加权合成 | LightGBM/XGBoost/TabNet非线性预测 | 🔴 高 |
| 57个因子（数量有限） | Alpha158/360因子池 | 🟡 中 |
| 无滚动模型重训 | Qlib滚动训练框架 | 🟡 中 |
| 因子间无正交化 | Gram-Schmidt正交化 | 🟡 中 |

### 结论：✅ 作为辅助/补充

**不建议替换**：你的因子生命周期管理、评估管线、Sleeve组合模式是核心IP，Qlib不具备等效功能。

**建议集成**：
1. `alpha/model.py` 新增 `combine_mode="qlib_lgb"` 分支
2. 评估管线Phase 2加入ML vs 线性IC加权的对比
3. Qlib因子作为补充候选池，与你现有因子并行评估

---

## 总体决策

| 优先级 | 模块 | 动作 | 范围 |
|---|---|---|---|
| 🥇 | vnpy | 替换执行层底部 | 最小侵入：仅替换`engine.execute()` → 真实券商下单 |
| 🥈 | Qlib | 辅助ML模型 | 新增加分支，并行评估，可随时回退 |
| ❌ | NautilusTrader | 暂不替换 | 仅在需要日内/分钟回测时重新评估 |

## 下一步

见 [ADR-036: vnpy执行层集成方案](ADR-036-vnpy-execution-integration.md)
