# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

---
---

## 工作纪律 (每次改动必须遵守)

### HANDOFF 文档
- 路径: `docs/handoffs/HANDOFF.md`
- **每次代码改动后必须更新**，记录：版本号、变更内容、原因、涉及文件、设计原则
- 每次 compact / 重启后第一步：读取 HANDOFF.md 了解最近变更

### 代码编辑
- 禁止 `sed` — 用 heredoc (`python3 << 'PYEOF'`) 或 `apply_patch`
- 禁止 fallback (静默降级掩盖错误)

### 版本号
- 格式: `test-v{N}`, 在 `web/app.py` 的 `VERSION` 变量
- 每次修改后递增


## 🚨 关键规则（每次改代码前必读，compact 后第一件事就是重读这里）

### 编辑工具
- **只用 heredoc 或 apply_patch 编辑文件**。严禁用 sed 按行号修改（行号总会漂移）。
- heredoc 模板: `cat > file.py << 'PYEOF' ... PYEOF`

- **YAML 文件禁止 apply_patch** — context 行空格前缀叠加 YAML 缩进会产生偏移。必须用:
  ```bash
  python3 << 'PYEOF'
  import yaml
  with open('path.yaml') as f: cfg = yaml.safe_load(f)
  cfg['key'] = value
  with open('path.yaml', 'w') as f: yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
  PYEOF
  ```
- **VERSION 行禁止 apply_patch** — 改用 re.sub 精确替换:
  ```bash
  python3 << 'PYEOF'
  import re; src = open('web/app.py').read()
  src = re.sub(r'VERSION = "test-v\d+"', 'VERSION = "test-v{N}"', src)
  open('web/app.py', 'w').write(src)
  PYEOF
  ```

### 零 Fallback（硬约束）
- **严禁 fallback**。任何 `try/except` 捕获后不允许降级返回默认值或跳过错误。
- 配置读取统一用 `_require_cfg("key")`，key 缺失 → KeyError → fail-fast。示范:
  ```python
  from quant.config.constants import _require_cfg
  value = _require_cfg("factor.min_abs_ic")  # 缺 key 即崩
  ```
- 业务代码禁止直接调用 `config.loader.get()`（该函数仅作 `_require_cfg` 底层实现）。

### DB 路径（全局常量）
- 所有数据库路径: `quant/data/`（项目根目录下，非 `data/`）
- 代码中用 `PROJ` 变量或 `pathlib.Path(__file__).resolve().parents[1]` 推导，不硬编码绝对路径
- 建表统一在对应 repo 类的 `_ensure_tables()` 中，禁止散落在一处性脚本里

### 导入规范
- 统一使用 `from quant.X import Y`（quant 前缀），禁止 `from X import Y`

### 日志
- 所有日志落盘到 `logs/` 目录，不散落各处
- 日志格式: `get_logger("quant.module.name")`，trace_id 通过 contextvars 贯穿
- 埋点: 函数入口/出口/异常 三处必须有 logger

### 版本号
- Web 版本号在 `web/app.py` 顶部 `VERSION = "test-vXX"`
- 每次修改完代码后推进版本号，并在终端告知用户

### 重启与命令执行（职责分工）
- **用户**执行: `bash scripts/restart.sh`（重启）、`bash scripts/run_task.sh <task> <date>`（手动跑任务）、crontab 安装
- **Agent**只给出命令文本，不执行这些命令

### 改代码前置步骤
1. 先读相关代码和文档（docs/ 下对应 ADR、HANDOFF.md）
2. 确认现有 pattern — merge/overlay/pipe，不得发明新模式
3. 对照 `coding-standards` SKILL.md 的「代码修改清单」逐项确认
4. 再动手改

### 用户约束（速查）
| 规则 | 说明 |
|------|------|
| 零 fallback | try/except 不降级，不吞错误 |
| heredoc/edit | 不用 sed 按行号；YAML/VERSION 禁 apply_patch |
| 读文档先 | 改代码前必读相关 doc |
| 版本号 | 每次修改推进 test-vXX；用 re.sub，禁 apply_patch |
| 重启 | Agent 只给命令文本，用户执行 |

### 编辑后验证（硬约束）
- **每次文件编辑后必须**:
  1. `python3 -c "import ast; ast.parse(open('修改文件.py').read())"` 语法检查
  2. 新增函数: `grep -c def 函数名 文件.py` + `python3 -c "import hasattr; ..."` 确认方法存在
  3. 修改变量顺序: 确认引用前已定义

### API 假设先测（硬约束）
- 涉及外部 API 的参数（batch_size, rate_limit, timeout等）→ **先写小脚本验证**，再写业务代码
- 禁止拍脑袋设 batch_size / sleep 值

### heredoc 深度 ≤1（硬约束）
- **禁止嵌套 heredoc**（pyEOF 内再写 pyEOF）
- 复杂字符串内容先写临时文件再读入替换


## Project overview

A股量化选股系统。基于 Grinold & Kahn Fundamental Law 的 7 层架构：数据 → 因子 → Alpha → 风控 → 优化 → 执行 → 监控。¥5,000 起步，目标 ¥100 万。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521)
PYTHONPATH=. python3 web/app.py

# 手动触发全流程
PYTHONPATH=. python3 pipeline.py

# 回测 (walk-forward)
PYTHONPATH=. python3 -c "from quant.backtest.loop import run_backtest; r=run_backtest('2024-01-01','2025-12-31',capital=5000); print(r['metrics'])"

# 因子评估
PYTHONPATH=. bash scripts/eval_standard.sh

# 因子缓存物化
PYTHONPATH=. python3 -c "from quant.scheduler.factor_cache import _run; _run('2026-07-28')"

# LightGBM 模型训练 (需 pip install lightgbm)
PYTHONPATH=. python3 -c "from quant.alpha.qlib_model import train_lgb_model; train_lgb_model()"

# 运行测试 (test/ 目录)
PYTHONPATH=. python3 -m pytest test/ -v

# 端到端验证
PYTHONPATH=. python3 scripts/validate_lgb_e2e.py
```

## Architecture
- **Schema 单源**: sim_trades/strategy_config DDL 只在 TradeRepo._ensure_tables() 一个地方定义。其他模块通过 TradeRepo 访问，不得自己开 sqlite3.connect 写入。engine.py 已清理，web/app.py 仅剩 api_performance/api_stats 只读查询。
- **Position dict keys**: `TradeRepo.get_positions()` 返回 dict 键为 `symbol, price, shares, board_count, buy_time`。没有 `value` 键 — 计算持仓市值必须用 `price * shares`。
- **Never hide stderr**: subprocess.run 不用 `stderr=subprocess.DEVNULL`。用 `stderr=subprocess.PIPE` 并在异常时打印。

(7 layers, ~25 files)

### Layer 0: Infra (`config/` + `utils/` + `execution/calendar.py`)
- `config/loader.py` — YAML 配置热加载
- `config/constants.py` — `_require_cfg()` 统一配置入口（key 缺失 → KeyError, fail-fast）
- `utils/logger.py` — `get_logger("module.name")`
- `utils/date.py` — `to_str()`, `to_compact()`, `today_str()`
- `execution/calendar.py` — `is_trading_day()`, `is_market_open()`, `get_trading_period()`

### Layer 1: Data (`data/`)
- `store.py` — **DataStore**: 多源日线增量同步（tickflow→新浪→腾讯→tushare→akshare），速度自适应轮转
- `trade_repo.py` — **TradeRepo**: `sim_trades` 统一读写，消除重复 SQL

### Layer 2: Factor (`factor/`) — 65因子计算 (57价格 + 8基本面)。运行时状态由 factor_registry 管理。实盘交易用 using (= active + monitoring)；回测评估用 backtesting；全量评估用 None。
- `factor/compute/` — 因子计算子包: price/ (动量/反转/波动率/量价/事件/情绪/另类) + fundamental.py (估值/质量/增长)。纯函数、向量化。
- `factor/compute/_primitives.py` — 52个共享预计算算子 (log_ret/cum_log/vol/roll_max等) + shortcut 映射
- `factor/compute/_dispatch.py` — compute_all_factors 调度器: 预加载→价格因子→基本面因子→季报真空期衰减
- `factor/compute/_preload.py` — 辅助数据预加载 (融资融券/分析师/龙虎榜/财务三表)
- `factor/compute/_registry.py` — 因子注册表 + 状态管理 (active/monitoring/candidate/retired/rejected)
- `factor/stats_cache.py` — IC计算 + Bayesian shrinkage + 因子快照缓存
- `factor/store.py` — **FactorStore**: 因子值物化缓存 (factor_cache.db)
- `factor/ic.py` — 截面 Spearman Rank IC + ICIR + 衰减分析
- `factor/synth.py` — 因子合成：`equal_weight()` / `ic_weighted()` / `sleeve_compose()`
- `factor/registry.py` — `_cs_zscore()`: winsorize 1%/99% + MAD 标准化 (ADR-037)
- `factor/orchestrator.py` — 因子评估编排
- `factor/state_manager.py` — 因子状态机生命周期

### Layer 3: Alpha (`alpha/`)
- `alpha/model.py` — **AlphaModel**: sleeve/composite/intersection/lgb 四种 combine_mode + sigmoid 软截断
- `alpha/synth.py` — sleeve_compose / ic_weighted / equal_weight / intersection_alpha / factor_attribution
- `alpha/rotation.py` — **SectorRotator**: 美林时钟行业轮动 (PMI+CPI → quadrant → 行业权重)
- `alpha/multi_tf.py` — **MultiTimeframeConfirmer**: 周线+日线多周期投票确认
- `alpha/qlib_model.py` — **LgbAlphaModel**: LightGBM 非线性 alpha 预测 (ADR-035 Phase 2)

### Layer 4: Risk (`risk/`)
- `risk/neutralize.py` — industry/size/style 中性化: 截面回归取残差
- `risk/covariance.py` — Ledoit-Wolf 收缩协方差 (2004) + HRP 层次风险平价
- `risk/constraints.py` — **RiskLimits**: 单票仓位上限/行业暴露上限/流动性门槛/ST过滤/涨停封死过滤
- `risk/var.py` — VaR 计算 (参数法 + 历史模拟)
- `risk/atr.py` — ATR 辅助计算

### Layer 5: Optimizer (`optimizer/`)
- `optimizer/portfolio.py` — **PortfolioConstructor**: 资本自适应三层
  - Nano (<¥30K): rank_concentrated 贪心集中
  - Micro (¥30K-100K): score_weighted 得分倾斜
  - Small (>¥100K): Kelly/mean-variance/HRP/risk_parity
  - §8.3 Grinold α−λ·TC 成本带换仓拦截
- `optimizer/rebalance.py` — `compute_trades()`: diff 目标持仓 vs 当前持仓 → 买卖订单
- `optimizer/kelly.py` — Kelly 公式仓位分配
- `optimizer/hrp.py` — Hierarchical Risk Parity (De Prado 2016)
- `optimizer/hyperopt.py` — Optuna 超参优化

### Layer 6: Execution (`execution/`)
- `execution/engine.py` — **ExecutionEngine**: 订单执行 → trades.db, 除权检测, T+1 检查, 板块涨跌停
- `execution/execution_model.py` — **ExecutionModel** (Template Method): BacktestExecutionModel / LiveExecutionModel 共用链
- `execution/cost.py` — **CostModel**: 统一成本模型 (佣金万三 + 最低¥5 + 印花税万五(B-21: 2023-08-28减半) + 滑点千一 + Almgren-Chriss 冲击)
- `execution/impact.py` — Almgren-Chriss (2001) 市场冲击模型: sqrt(Q/V)
- `execution/stop_loss.py` — **RiskManager**: ATR(20)三重止盈止损 + 固定硬止损 + 冷却注册表
- `execution/quote.py` — `fetch_quotes()`: 腾讯主源 + 新浪备用 + 并行batch + 五档盘口
- `execution/calendar.py` — 交易日历: akshare + 手动节假日 + rebalance_day 判定
- `execution/market_microstructure.py` — Roll(1984) 有效价差估计
- `execution/broker_adapter.py` — **BrokerAdapter** (ADR-036): SimulatedAdapter / VnpyCtpAdapter / VnpyXtpAdapter

### Layer 7: Monitor (`monitor/`)
- `monitor/attribution.py` — Brinson 归因 + IC 衰减自动检测
- `monitor/report.py` — 日报生成 + Web 推送
- `monitor/alerts.py` — 告警系统
- `monitor/factor_attribution.py` — 因子贡献归因
- `monitor/metrics.py` — 性能指标埋点
- `monitor/notify.py` — 通知推送

### 非 Layer 模块
- `backtest/` — 四层回测引擎 (loop/broker/bridge/analyze/naming)
- `evaluation/` — 8阶段评估管线 (Phase1-8: 数据→单因子→OOS→成本→监控→回测→Walk-forward→实盘一致性)
- `regime/` — HMM 3状态市场体制检测 (bull/bear/sideways) + 条件因子权重
- `benchmark/` — 基准跟踪
- `scheduler/` — 日频任务编排器 (orchestrator/evening/weekly/execute/monitor/factor_cache/order_manager)

## Data flow

```
quant/scheduler/ (单线程编排器: 08:30 信号 → 09:30 执行 → 09:35-11:30,13:00-14:55 盘中风控(午休跳过) → 15:30 归因+IC衰减检测)
  └─ pipeline.py.generate_signals(date)  ← 阶段一: 盘前信号生成
       ├─ Step 0: ExecutionEngine + PortfolioConstructor 初始化
       ├─ Step 1: DataStore.update_daily() 增量同步
       ├─ Step 2: UniverseRepo + risk pre-filters → investable universe
       ├─ Step 3: FactorStore.load() 读取因子缓存 → AlphaModel.combine() → AlphaModel.rank()
       ├─ Step 4: neutralize() + covariance_matrix(Ledoit-Wolf) + VaR check
       ├─ Step 5: PortfolioConstructor.construct() → target_positions
       └─ Step 6 (另开): execute_signals() → ExecutionModel.run() → 执行交易

  └─ pipeline.py.execute_signals()   ← 阶段二: 开盘执行
       ├─ ExecutionModel.run() (BacktestExecutionModel / LiveExecutionModel)
       ├─ 冷却过滤 → 硬止损 → delta → validate+alpha裁剪 → 成交
       └─ Step 7: generate_report() → push_to_web()
```

Each step has independent try/except — failure in one layer does not block later layers.

## Test suite

```bash
# 221 tests (test/ directory, not tests/)
PYTHONPATH=. python3 -m pytest test/ -v

# 运行特定模块
PYTHONPATH=. python3 -m pytest test/test_broker_adapter.py -v   # 35 broker tests
PYTHONPATH=. python3 -m pytest test/test_execution.py -v        # 8 execution tests
```

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Factor normalization | Winsorize 1%/99% + MAD z-score | Barra USE4; more robust than mean/std (ADR-037) |
| Alpha synthesis | Sleeve (default) / IC-weighted / LGB | Sleeve preserves factor independence; LGB for nonlinear (ADR-035) |
| Factor evaluation | 8-phase pipeline + Spearman Rank IC | CPCV + walk-forward + PBO; industry standard |
| Covariance | Ledoit-Wolf shrinkage | Better than sample for high dim (~5000 stocks × 60d) |
| Portfolio construction | Capital-adaptive 3-tier | Nano(<¥30K)/Micro(¥30-100K)/Small(>¥100K); auto-upgrades |
| Execution model | Template Method (Backtest/Live shared chain) | Backtest and live share same delta/validate/trim logic (ADR-036) |
| Broker integration | Adapter pattern (simulated/vnpy) | Swap config key to switch; zero-impact default (ADR-036) |
| Cost model | Unified CostModel (commission+stamp+impact) | Almgren-Chriss impact; stamp tax 0.05% since 2023-08-28 (B-21) |
| Performance | 52 precomputed primitives + factor cache | Eliminates redundant rolling stats; shortcut map for speed |

## Logging convention

```python
from utils.logger import get_logger
logger = get_logger("module.name")
```

## Factor evaluation commands

```bash
# L1+L2 快速评估 — active 因子
PYTHONPATH=. bash scripts/eval_layer12.sh
# L1+L2+L3 完整评估 (读写 factor_registry.status)
PYTHONPATH=. bash scripts/eval_stepwise.sh
# 五阶段标准评估 (CPCV + walk-forward + PBO, 业界标准)
PYTHONPATH=. bash scripts/eval_standard.sh
```

## Key docs

| 文档 | 内容 |
|------|------|
| `docs/adr/` | Architecture Decision Records (37+) |
| `docs/adr/ADR-035-infrastructure-replacement-analysis.md` | 基础设施替换分析 (NautilusTrader/vnpy/Qlib) |
| `docs/adr/ADR-036-vnpy-execution-integration.md` | vnpy BrokerAdapter 集成方案 |
| `docs/adr/ADR-037-factor-audit-fixes.md` | 因子计算审计修复 (winsorize+MAD/衰减/注释) |
| `docs/architecture/` | Data dictionary, data sources |
| `docs/research/` | Factor research papers |
| `docs/reports/` | Audit and analysis reports |
| `HANDOFF.md` | 变更日志 (每次改动后更新) |

## Data quirks (not bugs)

### Cash balance ≠ initial_capital - Σ(stock_cost)
差额是交易成本：佣金(万三，最低¥5/笔) + 印花税(万五, 2023-08-28减半) + 滑点(千一，双向)。CostModel 在 `execution/cost.py`。
验证方法：
```python
python3 -c "
c = __import__('execution.cost', fromlist=['CostModel']).CostModel()
trades = [(200, 10.60), (100, 18.49)]
print(sum(c.buy_cost(s, p) - s*p for s,p in trades))  # = 13.97
"
```

## Coding rule: Read before design

Before proposing any solution:
1. **Read the target method/function** — the exact code path that will be modified
2. **Identify the existing pattern** — merge, overlay, pipe, fallback, etc.
3. **Fit the change into that pattern** — minimum addition, same shape

If the existing code already has a merge/overlay step, add to it. Never design alternatives before reading.

## Workflow Rule: 回测后自动分析日志

**用户回测完成后**，agent 必须主动检查日志文件，不得等待用户粘贴错误。

**触发词**: "跑完了" / "done" / "回测跑完了" / "测试跑完了"

**Agent 必须执行**:
1. grep ERROR 最近 20 条
2. 如有错误，按 trace_id 回溯上下文
3. 检查 WARNING 中是否有新的非边界告警

**职责分工**:
- 终端 (stdout): INFO+ — 用户确认代码在跑、进度、诊断结论
- logs/quant.log: DEBUG+ (全量) — agent 抓 ERROR/WARNING 定位 bug

**已知无害 WARNING (不需报告)**:
- post_state failed N consecutive times — 回测时无 Flask 服务
- no open prices available, skipping — 最后一天无次日开盘价
- empty common universe / insufficient common stocks — 正常边界
- T+1 blocked — 正常风控拦截
