# Handoff: quant 项目状态 — 2026-07-07 02:00 CST

## 进入检查清单

| # | 检查项 | 命令/方法 |
|---|--------|----------|
| 1 | 先看日志 | tail -30 logs/quant.log |
| 2 | 服务存活 | lsof -i:8521 |
| 3 | 活跃因子 | `sqlite3 data/market.db "SELECT name,category FROM factor_registry WHERE status='active'"` |
| 4 | 最新 commit | git log --oneline -5 |

## 最新 5 个 commit

```
5d71470 P64: 集成 4 个 A 股已验证新因子 — 资产增长 + 毛利/资产 + 停牌比率 + 北向资金
5025a2a docs: P63 文档同步 — CHANGELOG + CLAUDE + HANDOFF
2da6f6d P63: 优化器参数去硬编码 — 资本分层自动判定 + risk_aversion 实时校准
49189ce P62 hotfix: 恢复 factor.stats 被 P60 覆盖的两个 key
216554d docs: 同步所有文档至最新状态
```

## P64 改动详情

### 背景
现有 36 因子池中仅 2 个有效 (zt_streak, dt_streak)。不再继续挖掘剩余 34 个。
转向集成 A 股已被量化公司/软件/文献验证有效的新因子。

### 落地 4/10 因子 (Step 1: 数据源就绪)

| 因子 | 类别 | 来源 | 数据 | 预期 IC |
|------|------|------|------|---------|
| `asset_growth` | fundamental | Cooper, Gulen & Schill (2008) | financial_balance.total_assets | 负 |
| `gp_ta` | profitability | Novy-Marx (2013) | financial_income + financial_balance | 正 |
| `ztd` | liquidity | Liu (2006) | daily.volume (零成交量计数) | 负 |
| `northbound_20d` | northbound | 华泰 2023 | northbound_flow.net_buy | 正 |

### 待续
- Step 2: SUE (缺 stocks.total_shares 列)
- Step 3: 大股东减持 / 股权质押 / 股息率 (需新建 Tushare 数据模块)
- 完整候选清单: docs/adr/023-new-factor-candidates.md

## 当前运行状态

- 种子资金: ¥5,000 (TradeRepo 持久化)
- 活跃因子: 6 (zt_streak, dt_streak, asset_growth, gp_ta, ztd, northbound_20d)
- 总因子数: 40 (36 original + 4 new)
- 数据库: SQLite WAL 模式, busy_timeout=30s
- 日志: logs/quant.log (按日期滚动, 保留 10 天)

## 启动命令

```bash
cd /Users/mariusto/project/quant && ./restart.sh
```
