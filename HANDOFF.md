# Handoff: quant 项目状态 — 2026-07-07 03:20 CST

## 进入检查清单

| # | 检查项 | 命令/方法 |
|---|--------|----------|
| 1 | 先看日志 | tail -30 logs/quant.log |
| 2 | 服务存活 | lsof -i:8521 |
| 3 | 活跃因子 | `sqlite3 data/market.db "SELECT name,category FROM factor_registry WHERE status='active'"` |
| 4 | 最新 commit | git log --oneline -5 |

## 最新 5 个 commit

```
7125743 P67: 数源切换 Tushare→akshare
68fcd09 P66: 集成 3 个机构/风险/价值新因子 — 大股东减持 + 股权质押 + 股息率
ae98ffa P65: SUE 因子落地 — 标准化未预期盈余 (PEAD)
80580c3 docs: P64 文档同步 — CHANGELOG + HANDOFF
5d71470 P64: 集成 4 个 A 股已验证新因子
5025a2a docs: P63 文档同步
```

## P64-P66 改动总结

| 版本 | 内容 | 文件 |
|------|------|------|
| P63 | optimizer 去硬编码: 资本分层自动判定 + risk_aversion 实时校准 | optimizer/portfolio.py, pipeline.py, config.yaml |
| P64 | 4 新因子: asset_growth, gp_ta, ztd, northbound_20d | factor/compute.py |
| P65 | SUE 因子 + total_shares 列 | factor/compute.py, data/fundamental.py |
| P67 | 数源切换: holder_trade/pledge_stat Tushare→akshare | data/holder_trade.py, data/pledge.py, factor/compute.py |
| P66 | 3 新因子: holder_reduction, pledge_ratio, dividend_yield + 3 个 Tushare 数据模块 | factor/compute.py, data/holder_trade.py, data/pledge.py, data/dividend.py |

## 当前运行状态

- 种子资金: ¥5,000 (TradeRepo 持久化)
- 活跃因子: 10 (asset_growth, dividend_yield, dt_streak, gp_ta, holder_reduction, northbound_20d, pledge_ratio, sue, zt_streak, ztd)
- 总因子数: 44
- 数据库: SQLite WAL 模式, busy_timeout=30s
- 日志: logs/quant.log (按日期滚动, 保留 10 天)

## 待办

- holder_trade/pledge_stat 改用 akshare (免费), 命令见下
- 运行 `PYTHONPATH=. bash scripts/eval_layer12.sh` 测试新因子 IC 表现
- 完整候选清单: docs/adr/023-new-factor-candidates.md

## 启动命令

```bash
cd /Users/mariusto/project/quant && ./restart.sh
```
