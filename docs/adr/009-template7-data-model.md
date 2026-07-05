---
adr: 009
date: 2026-07-05
status: accepted
---

# ADR 009: 模板 7 数据模型 — 硬约束 + SQLite 量化特例

## 背景

模板 7（数据模型设计）原样套用对 SQLite 量化项目存在三个问题：
1. 3NF 强制拆表会导致因子计算 JOIN 5-6 张表，性能退化 10x+
2. Alembic 对 SQLite 的支持残废（不支持 DROP COLUMN / ALTER COLUMN TYPE）
3. UUID 外部 ID 无消费者但增加存储开销

同时数据库实际存在严重问题：
- `sim_trades.side` 无 CHECK 约束（TEXT 存 buy/sell 却无校验）
- 三张财务表只有主键索引，按 `stat_date` 查询全表扫描 21K 行
- 11 张表无 `created_at` 时间戳
- 财务表建表语句散落于一次性脚本（已删除），无模块认领

## 决策

**模板 7 保留为硬约束，加三条 SQLite 量化特例：**

| 规则 | 处置 | 理由 |
|------|------|------|
| 字段类型/索引/时间戳/每表有主 | 硬约束 | 修复当前所有实际缺陷 |
| 3NF | 分析宽表豁免；交易/配置表仍范式化 | 分析型负载 JOIN 代价过高 |
| Alembic | 替换为 `docs/migrations/NNN.sql` + 幂等 ALTER | SQLite DDL 限制 |
| UUID | 仅 API 暴露层需要 | 内部表无消费者 |

## 落地

- `data/trade_repo.py`: sim_trades 加 `CHECK(side IN ('buy','sell'))` + `created_at`
- `data/jq_financials.py`: 新建模块认领三张财务表（balance/income/cash_flow），`ensure_tables()` + upsert
- `docs/migrations/001-baseline.sql`: 审计前 schema 基线
- `docs/migrations/002-template7-audit.sql`: 加索引 + 约束迁移 SQL
- `docs/coding-standards-SKILL.md`: 模板 7 更新

## 后果

- 财务表按 `stat_date` 查询不再全表扫描（索引生效）
- 每张表有明确的所有者模块，不再有无主表
- `sim_trades.side` 写入非法值会被 SQLite 拒绝
- 新表建表须遵守：`ensure_tables(conn)` 幂等模式 + `created_at` 时间戳 + 覆盖主查询的索引
