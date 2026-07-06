# Handoff: quant 项目状态 — 2026-07-05 22:15 CST

## 本次会话（2026-07-05）

### P38-P42: 参数配置化 + 回归修复
- P38: 15 个遗漏参数迁入 config.yaml → 引入 6 个 regression bug
- P39: 6 个参数从开发测试值切换为生产标准
- P40-P42: 全部回归修复（cfg/_cfg mismatch、rebalance_dates 排序、f-string 冲突、line27 artifact）
- 20/20 测试通过，eval_stepwise 三级评估正常

### P43: 多因子分仓架构
- config.yaml: \`alpha.combine_mode: sleeve\` (默认) + \`alpha.sleeve\` 节
- factor/synth.py: \`sleeve_compose()\` — 每因子独立 top N 取并集
- pipeline.py: \`combine_mode\` 分支 (sleeve / composite)，soft cutoff 跳过 sleeve
- 合成模式: composite (加权压缩) | sleeve (分仓保留独立信号)
- sleeve 修复: \`method='sleeve'\` 避免 UnboundLocalError
- 核心路径埋点: synth per-factor 日志 + pipeline sleeve 明细 + backtest 逐调仓 PnL
- ADR 017: 多因子分仓架构决策记录
- **sleeve_compose 返回值变更**: 旧=等权 1.0 mask, 新=原始 z-score (保留信号梯度)
- 多因子碰撞规则: 同一股票被多个因子选中时取 max z-score
- tests/test_synth.py: 6 个 sleeve_compose 测试 (26/26 全通过)

### P45: 因子评估死锁修复 — 计算层架构分离
- ADR 018: 识别死锁 (deprecated 因子永不可恢复, 34/35 被排除)
- ADR 019: 架构修复方案 (Grinold & Kahn 两阶段分离)
- factor/compute.py: load_active_price_factors/fundamental_factors/get_factor_names 增加 status_filter 参数
- factor/compute.py: compute_all_factors 增加 factor_names 参数 (显式列表绕过 status)
- factor/stats_cache.py: compute_factor_stats 增加 factor_names 透传; load_ic_map_from_cache 加载全量 IC
- eval_stepwise.sh: Layer 1+2 前查询全部 35 个因子名, 传入 compute_factor_stats
- pipeline.py / validate.py: 不改 (生产默认 active, 校验不变)
- 26/26 测试通过
- **下次 eval 将评估全量 35 个因子, 而非仅 1 个 active**

### P44: 回测窗口标准化 — 扩展到 ≥250 天
- 3 个脚本硬编码 2026-01-01→2026-06-30 (116d) → 统一改为 config.yaml 默认值
- eval_stepwise.sh Layer 3: 读取 backtest.default_start/end/capital, timeout 300→600s
- backtest_jq.sh: 读取 config 默认值
- run_backtest_clean.sh: 读取 config 默认值
- 实际覆盖: 2023-01-01 → 2026-06-30 ≈ 846 个交易日, 远超 Grinold & Kahn 250 天最低标准
- 数据验证: DB 7.45M 行, 2020-2026 共 1,574 个交易日, 完整无缺口

### ADR 016: 数据源注册表
- 12 个数据源完整分析（tencent/baostock/tushare/JQData/akshare/sina/pytdx 等）
- 分层策略: 主力/备用/已排除
- 凭证管理: config/.env (TUSHARE_TOKEN, JQDATA_USER/PASS)

### 因子评估现状
- 当前 active: 1 个 (zt_streak, IC=+0.0424, t=7.1, IR=+0.65)
- n=800 / lookback=120 下 bp_ratio/size/gap_5d 不显著 (t<2.0)
- reversal_5d/volatility_126d/amihud_250d 不显著 (t<2.0)
- 每日估值数据 425,493 行，基本面数据完整

### 回测基线
- zt_streak composite 模式 (P42): 6个月 Wealth=~9,357, 3年 Wealth=~516,392
- zt_streak sleeve 模式 (P43): 6个月 Wealth=~2,417 (Sharpe=-0.401) — 单因子 sleeve 效果差于 composite

### 已知问题: sleeve 模式单因子回测异常
- 单因子 sleeve (zt_streak only) 回测 Wealth=~2,417 vs composite 的 ~9,357
- 根因: sleeve 严格限定 positions_per_factor=15 只选出 15 只股票, 集中度极高
- composite 选出 50+ 只股票后由 risk layer 做组合优化, 分散度好
- **待讨论**: 单因子场景下 sleeve vs composite 的语义差异是否需要特殊处理

## 项目路径
/Users/mariusto/project/quant
Python: .venv (3.14.6), .venv-tushare (3.12)
Redis: localhost:6379
测试: pytest tests/ -q (26/26)
DB: data/market.db (7,454,629 行 daily, 2020-01-02 ~ 2026-07-03, 1,574 交易日)
Git: origin/main @ 17a1377

## 待完成
1. **跑 eval_stepwise.sh** — 验证全量 34 因子评估 (P45 修复), 预期 dt_streak/gap_5d/size/bp_ratio 等重新进入候选
2. 多因子 sleeve 组合评估 (需要 ≥3 个候选因子才有意义)
3. lookback 参数扩展 (120→500, 匹配 alpha.train_window)
4. benchmark.py 添加 fallback（sina DNS 偶发失败）
5. 新因子开发方向: LHB/margin/northbound 数据已有但对应 compute 函数待审查
6. sleeve vs composite 单因子场景语义 — 是否需要自动回退到 composite

### ADR 020: 151 Trading Strategies — 核心理论与公式提取
- 覆盖 Ch.3 Stocks 核心策略: Price-Momentum, Value, Low-Vol, Residual Momentum, Mean-Reversion Weighted Regression, Multifactor, Risk Model, Alpha Combos
- 每个策略标注了标准参数 (来源于文献) 及与项目因子的对照
- 识别出 3 个关键缺口: momentum_10d 窗口过短 (10d vs 63d+), reversal_5d 方法弱 (单变量 vs 加权回归), 缺失 Residual Momentum (Ch.3.7)

### ADR 021: 151 Trading Strategies — 股票策略完整参考手册
- 21 个策略的完整分类、公式、交易规则、标准参数、关键文献 (525 行)
- Ch.3 原文逐策略提取 + 公式索引 (Eqs 1-95)
- 项目缺失因子优先级排序 (P0: Residual Momentum, P1: Momentum 63d+/SUE, P2: Weighted Mean-Reversion)
- 与 ADR 020 互补: 020 是分析+行动建议, 021 是完整参考手册

### ADR 022: 可落地四档方案 + 波动率拖累诊断
- Sharpe 0.843 与 -82% 收益同时出现: 根因是波动率拖累 (σ=13.5%日波动, 算术日均 +0.72% 但方差减损 -0.91%)
- 信号有效 (zt_streak+dt_streak), 仓位管理崩溃级 (σ=沪深300的11倍)
- 四档推进: 第一档(改参数)→第二档.1(动量变体)→第二档.2(残差动量)→第三档(算法升级)

## 2026-07-06 ~08:42

### P48: 消除全部静默 except-pass 模式
- **问题**: 前次审计聚焦 return-value fallback 和 DB schema mismatch，遗漏了 10 个数据同步函数中的行级 `except Exception: pass`
- **修复**: 9 个 data/*.py sync 函数 → `except Exception as e_row: logger.debug(...)`；monitor/report.py push_to_web → logger.debug；jq_valuation.py config load → logger.warning
- **额外 bug**: data/store.py:356 `except Exception:` 缺少 `as e` 但 f-string 引用了 `{e}`，会导致 NameError
- **验证**: 全代码扫描确认 0 剩余未标注的静默吞异常；0 DB schema-query 列名不匹配
- **4 个保留的 bare except-pass**: backtest.py:99 (stop-loss optional), pipeline.py:199 (benchmark optional), store.py:317 (bs.logout cleanup), cache.py:259 (backend probe chain) — 均有注释或为级联探针逻辑

### 当前系统状态
- Web app: 8521 端口运行中 (PID 43441)
- 因子: 35 注册 (1 active: zt_streak)
- 本金: ¥5000 已入库 trades.db → strategy_config.initial_capital
- 所有关键路径均已埋点 (pipeline, synth, backtest, data sync)

## 2026-07-06 ~09:00

### P49: Pipeline 两阶段分离 + 三时段调度 + 策略隔离 (ADR 024)

**问题**: 原有 pipeline 在 15:30 盘后一步完成信号生成+执行，用当日收盘数据产生信号并在收盘价"成交"——因果颠倒（未来函数）。且 manual/quant 策略交易混在同一张表，无隔离。

**架构变更**:
- `pipeline.py`: 拆为 `generate_signals()` (Steps 0-5, 只产目标持仓不执行) + `execute_signals()` (Steps 6-7, 开盘价 delta 执行)
- `scheduler.py`: 三时段 — 08:30 信号 → 09:30 执行 → 15:30 归因
- `web/app.py` `/api/trade`: 修复 strategy 未定义 NameError bug, 所有手动交易固定 `strategy='manual'`
- `web/app.py` `/api/quotes`: 集成止损扫描, 复用现有 5s 报价路径, 无额外定时器
- `execution/engine.py`: 无需改动, 已支持 strategy 过滤和开盘价执行

**策略隔离**:
| strategy | 来源 | 用途 |
|---|---|---|
| `quant` | pipeline 自动 | 纸盘模拟, 全自动执行 |
| `manual` | 前端手动录单 | 券商户实盘追踪 |

### 当前系统状态
- Web app: 8521 端口 (需重启以应用 /api/trade 修复和 /api/quotes 止损)
- 因子: 35 注册 (1 active: zt_streak)
- 本金: ¥5000 strategy_config.initial_capital
- 交易日流程: 08:30 信号 → 09:30 执行 → 盘中 5s 报价+止损 → 15:30 归因

## 2026-07-06 ~09:05

### P50: Scheduler 统一日志格式
- 每阶段增加 `[SCHEDULER] YYYY-MM-DD | PHASE=N | STATUS=OK/FAIL | ...` 日志行
- 事后排查: `grep 'SCHEDULER.*STATUS=FAIL'` 一键定位所有出问题的交易日

## 2026-07-06 ~09:15

### P51: restart.sh — 代码更新后快速重启
- `restart.sh`: 一键停旧 → 启 web → 重载 launchd scheduler
- 解决 launchd `KeepAlive` 不检测代码变更的问题

### P52: daily_sync bug 修复
- `data/store.py`: `cfg` import 在 `if start is None` 内但 `batch_size=cfg(...)` 在外，Python 3.14 报 `cannot access local variable cfg where it is not associated`。将 import 移到 if 之前。
- `daily_sync.py`: `step5_fundamentals()` 调用 `sync_all(max_fetch=500)` 缺少必传参数 `conn`。补建连接传入。

