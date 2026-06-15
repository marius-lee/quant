# 数据库建表规范审查

日期: 2026-06-05

## 数据库概览

| 数据库 | 大小 | 表数 | 用途 |
|------|:--:|:--:|------|
| market.db | 3.7GB | 6 | OHLCV + 因子 + 股票列表 |
| results.db | 1MB | 9 | 分析结果 + 模拟交易 |
| live.db | <1MB | 6 | 实盘交易 |

## 审查结果

### 已修复

| 表 | 问题 | 修复 |
|------|------|------|
| `factors` | 无主键, TEXT 存数字 | REAL 类型 + PK(stock,date) |
| `factors` | 中间列冗余 | 删 high/low/volume/amount/open |
| `daily` | idx_daily_date 坏索引 | 已删除 |
| `positions` (results) | 无 CHECK 约束 | CHECK(status IN ('open','closed')) |
| `trade_history` | 无 CHECK 约束 | CHECK(side IN ('buy','sell')) |
| `live_positions` | 无 CHECK + 无时间戳 | CHECK + updated_at 列 |
| `orders` | max_price 死列 | 已删除 |
| `orders` | 无 CHECK | CHECK(side + status) |

### 新增索引

| 表 | 索引 | 目的 |
|------|------|------|
| `runs` | (strategy, id) | `/api/latest` 按策略查历史 |
| `picks` | (run_id, rank) | 加载推荐列表 |
| `orders` | (symbol, status) | 防重查询 |
| `live_trades` | trade_date | 今日成交查询 |
| `tracking` | rec_date | 追踪查询 |

### 不建议的修改

- `daily` 加自增 id: INSERT OR IGNORE 去重机制依赖 (symbol,date) 唯一约束
- `positions` / `orders` status 单列索引: 选择性太差 (open/closed 只有 2 个值)

## 来源

- SQLite 索引选择性: 低选择性列 (如 2 值 status) 建索引无效
- CHECK 约束: SQL 标准, 防止脏数据写入
