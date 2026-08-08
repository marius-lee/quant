# 日终对账 (OMS Reconciliation)

## 数据模型

daily_recon 表 (market.db)，每日 reconcile 任务落库。

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | TEXT | `position`(持仓) / `cash`(现金) / `order`(订单) |
| `symbol` | TEXT | 股票代码 或 检查项标识 |
| `expected` | REAL | 期望值 |
| `actual` | REAL | 实际值 |
| `drift` | REAL | 差异 = expected − actual |
| `status` | TEXT | `ok`(正常) / `break`(异常) / `skip`(跳过) |

## 检查项说明

### position (持仓对账)
逐股比对 signals 目标股数 vs 实际持仓股数。
- drift=0 → ok
- drift≠0 → break

### cash (现金检查)
| symbol | 检查内容 | 异常条件 |
|--------|---------|---------|
| `invariant` | 现金余额 ≥ 0 | cash < 0 → 记账或裁剪逻辑 bug |
| `equity_cross` | 现金 × 总权益一致性 | 差异 > `recon.cash_drift_tolerance` |
| `pnl_cross` | 日盈亏 vs 交易记录 | v429 判定: 已由 equity_cross (流水推演) + order/filled 账本交叉核对覆盖, 不再独立实现 |

### order (订单检查)
pending_orders 状态 vs sim_trades 成交记录。
- 有挂单无成交 → break
- 有成交无挂单 → break

## 界面展示

调度页面 → 日终对账表格：

| 列 | 数据源 | 格式化 |
|----|--------|--------|
| 类型 | `kind` | position→持仓, cash→现金, order→订单 |
| 标的/检查 | `symbol` | invariant→现金≥0, equity_cross→现金×权益, pnl_cross→PnL交叉 |
| 期望 | `expected` | 数值或 — |
| 实际 | `actual` | 数值或 — |
| 差异 | `drift` | 数值或 — |
| 状态 | `status` | ok→正常(绿), break→异常(红), skip→跳过(灰) |

## 触发时机

- 每日 15:05 reconcile 任务
- 仅调仓日执行 (非调仓日信号不更新，无对账意义)
