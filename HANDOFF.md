# Handoff: quant 项目状态 — 2026-07-05 19:45 CST

## 本次会话（2026-07-05）

### P38-P42: 参数配置化 + 回归修复
- P38: 15 个遗漏参数迁入 config.yaml → 引入 6 个 regression bug
- P39: 6 个参数从开发测试值切换为生产标准
- P40-P42: 全部回归修复（cfg/_cfg mismatch、rebalance_dates 排序、f-string 冲突、line27 artifact）
- 20/20 测试通过，eval_stepwise 三级评估正常

### P43: 多因子分仓架构
- config.yaml: `alpha.combine_mode: sleeve` (默认) + `alpha.sleeve` 节
- factor/synth.py: `sleeve_compose()` — 每因子独立 top N 取并集
- pipeline.py: `combine_mode` 分支 (sleeve / composite)，soft cutoff 跳过 sleeve
- 合成模式: composite (加权压缩) | sleeve (分仓保留独立信号)
- sleeve 修复: `method='sleeve'` 避免 UnboundLocalError
- 核心路径埋点: synth per-factor 日志 + pipeline sleeve 明细 + backtest 逐调仓 PnL
- ADR 017: 多因子分仓架构决策记录
- tests/test_synth.py: 6 个 sleeve_compose 测试 (26/26 全通过)

### ADR 016: 数据源注册表
- 12 个数据源完整分析（tencent/baostock/tushare/JQData/akshare/sina/pytdx 等）
- 分层策略: 主力/备用/已排除
- 凭证管理: config/.env (TUSHARE_TOKEN, JQDATA_USER/PASS)

### 因子评估现状
- 当前 active: 1 个 (zt_streak, IC=+0.0424, t=7.1, IR=+0.65)
- n=800 / lookback=120 下 bp_ratio/size/gap_5d 不显著 (t<2.0)
- reversal_5d/volatility_20d/amihud_20d 不显著 (t<2.0)
- 每日估值数据 425,493 行，基本面数据完整

### 回测基线
- zt_streak composite 模式 (P42): 6个月 Wealth=~9,357, 3年 Wealth=~516,392
- zt_streak sleeve 模式 (P43): 待重新验证（method 修复后）

## 项目路径
/Users/mariusto/project/quant
Python: .venv (3.14.6), .venv-tushare (3.12)
Redis: localhost:6379
测试: pytest tests/ -q (26/26)
DB: data/market.db (7,454,629 行 daily, 2020-01-02 ~ 2026-07-03, 1,574 交易日)
Git: origin/main @ ee8a28b

## 待完成
1. 重新跑 eval_stepwise.sh 验证 sleeve 模式 Wealth 正常
2. 恢复 zt_streak + dt_streak 双因子 sleeve 组合评估
3. benchmark.py 添加 fallback（sina DNS 偶发失败）
4. 新因子开发方向: LHB/margin/northbound 数据已有但对应 compute 函数待审查
