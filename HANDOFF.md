# Handoff: quant 项目状态 — 2026-07-07 01:30 CST

## 进入检查清单

| # | 检查项 | 命令/方法 |
|---|--------|----------|
| 1 | 先看日志 | tail -30 logs/quant.log |
| 2 | 服务存活 | lsof -i:8521 |
| 3 | 界面 KPI | 总资产/PnL/PnL%/胜率/交易次数(买+卖)/可用资金/持仓市值 |
| 4 | 最新 commit | git log --oneline -5 |

## 最新 5 个 commit

```
2da6f6d P63: 优化器参数去硬编码 — 资本分层自动判定 + risk_aversion 实时校准
49189ce P62 hotfix: 恢复 factor.stats 被 P60 覆盖的两个 key
216554d docs: 同步所有文档至最新状态
baa8b9d P62: factor_registry 修复 + validate 改进
a0ec63e P61: 审计收尾 — 消除最后 6 项硬编码 + neutralize 路径统一
```

## P63 改动详情

### 问题
1. `equal_weight_cap=20000` / `weighted_cap=100000` 是拍脑袋的经验阈值, 无学术依据
2. `risk_aversion=2.0` 硬编码默认值, 违背「每个数值必须有来源」原则
3. `cfg("key", default)` 的 fallback 模式制造隐形 bug

### 方案
1. **资本分层自动判定** — `_tier(capital, avg_price)`:
   - `capital < 均价×lot_size×2` → 贪心等权
   - `capital < 均价×lot_size×max_positions` → 得分倾斜
   - 否则 → 均值-方差
   - 阈值完全由当前资金量与股价决定, 零硬编码

2. **risk_aversion 实时校准** — `calibrate_risk_aversion()`:
   - 模块级独立纯函数, 不依赖 PortfolioConstructor 实例
   - λ ∈ {0.5, 1.0, 2.0, 5.0, 10.0} 网格搜索
   - 选 Sharpe 最优的 λ
   - 每次进入均值-方差分支时实时调用
   - 不在 config.yaml 中, 不在实例上存储

3. **pipeline.py** — `cov` 作用域从 Step 4 提出, 传入 `construct(covariance=cov)`

### 变更文件
- `config/config.yaml`: 删除 `equal_weight_cap: 20000`
- `optimizer/portfolio.py`: 重构 (+calibrate_risk_aversion, -三个硬编码参数)
- `pipeline.py`: cov 作用域提升 + 传入 construct()

## 当前运行状态

- 种子资金: ¥5,000 (TradeRepo 持久化)
- 活跃因子: 2 (zt_streak, dt_streak)
- 36 因子总数 (27 price + 9 fundamental)
- factor cache warming: ~30s 启动时后台执行
- 数据库: SQLite (WAL 模式, busy_timeout=30s)
- 日志: logs/quant.log (按日期滚动, 保留 10 天)

## 启动命令

```bash
cd /Users/mariusto/project/quant && ./restart.sh
```
