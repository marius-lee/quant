# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

---
---

## 工作纪律 (每次改动必须遵守)

### HANDOFF 文档
- 路径: `docs/handoffs/HANDOFF.md`
- **每次代码改动后必须更新**，记录：版本号、变更内容、原因、涉及文件、设计原则
- 每次 compact / 重启后第一步：读取 HANDOFF.md 了解最近变更

### 资金计算
- `sim_trades` 是资金唯一真相源，`get_cash()` 实时计算，从不缓存
- 禁止维护 `cash_balance` 之类的手动同步列
- 每笔交易必须存储 `cost` (佣金+印花税+滑点)

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
| heredoc/edit | 不用 sed 按行号 |
| 读文档先 | 改代码前必读相关 doc |
| 版本号 | 每次修改推进 test-vXX |

## Project overview

A股量化选股系统。基于 Grinold & Kahn Fundamental Law 的 7 层架构：数据 → 因子 → Alpha → 风控 → 优化 → 执行 → 监控。¥5,000 起步，目标 ¥100 万。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521)
PYTHONPATH=. python3 web/app.py

# 手动触发全流程
PYTHONPATH=. python3 pipeline.py

# 因子评估
PYTHONPATH=. python3 -c "from factor.ic import compute_ic; print(compute_ic(factor_names=get_factor_names()))"

# 运行测试
PYTHONPATH=. python3 -m pytest tests/ -v
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

### Layer 2: Factor (`factor/`) — 57因子计算函数 (41 price + 16 fundamental)。运行时状态由 factor_registry 管理。实盘交易用 using (= active + monitoring)；回测评估用 backtesting；全量评估用 None。
- `base.py` — **Factor** 抽象基类: `compute(data) → Series`, `evaluate(values, returns) → dict`
- `compute/` — 因子计算子包: price/ (动量/反转/波动率/流动性/事件/情绪/另类) + fundamental.py (估值/质量/增长)。纯函数、向量化。
- `ic.py` — 统一 IC 计算模块（双模式: Mode A 取数据算因子, Mode B 预计算因子值）。截面 Spearman Rank IC + ICIR + 衰减分析
- `synth.py` — 因子合成：`equal_weight()` / `ic_weighted()`

### Layer 3: Alpha (`alpha/`)
- `model.py` — **AlphaModel**: 因子合成 → 收益预测 → 截面分位数排名

### Layer 4: Risk (`risk/`)
- `neutralize.py` — `industry_neutralize()`, `size_neutralize()`: 截面回归取残差
- `covariance.py` — `ledoit_wolf_cov()`: 收缩协方差估计 (Ledoit & Wolf 2004)
- `constraints.py` — **RiskLimits**: 单票仓位上限、行业暴露上限、流动性门槛、ST 过滤

### Layer 5: Optimizer (`optimizer/`)
- `portfolio.py` — **PortfolioConstructor**: 资本自适应 (<2万等权 / 2-10万得分倾斜 / >10万均值-方差) + 整手约束
- `rebalance.py` — `compute_trades()`: diff 目标持仓 vs 当前持仓 → 买卖订单列表

### Layer 6: Execution (`execution/`)
- `engine.py` — **ExecutionEngine**: 订单执行 → trades.db + capital_after
- `cost.py` — **CostModel**: 统一成本模型（佣金万三 + 最低 5 元 + 印花税千一）
- `quote.py` — `fetch_quotes()`: 实时行情（腾讯主源 + 新浪备用）
- `calendar.py` — 交易日历

### Layer 7: Monitor (`monitor/`)
- `attribution.py` — Brinson 归因 + IC 衰减自动检测: active→monitoring (衰减>30%), monitoring→retired (持续≥10天)
- `report.py` — `generate_report()`: 日报 → JSON → Web 推送

## Data flow

```
quant/scheduler/ (单线程编排器: 08:30 信号 → 09:30 执行 → 09:35-14:55 盘中风控 → 15:30 归因+IC衰减检测)
  └─ pipeline.py.run(date)
       ├─ Step 1: DataStore.update_daily()
       ├─ Step 2: factor/ic.py → IC/IR report
       ├─ Step 3: alpha/model.py → predict → cross_sectional_rank
       ├─ Step 4: risk/neutralize.py + risk/constraints.py → filter
       ├─ Step 5: optimizer/portfolio.py → construct → TargetPortfolio
       ├─ Step 6: execution/engine.py → execute → trades.db
       └─ Step 7: monitor/report.py → push to web/shared.py
```

Each step has independent try/except — failure in one layer does not block later layers.

## Key design decisions

- **截面 Rank IC**: Spearman 秩相关评估因子预测力，对异常值鲁棒
- **Ledoit-Wolf 收缩**: 协方差估计优于样本估计，适合高维截面（~5000 股票 × 60 日）
- **资本自适应优化**: <2万等权 → 2-10万得分倾斜 → >10万均值-方差 + 整数约束。方法随资金增长自动升级
- **统一成本模型**: `CostModel` 是所有模拟交易的唯一成本入口，确保绩效可比
- **配置驱动**: 所有阈值从 `config.yaml` 读取，通过 `_require_cfg()` 取值，key 缺失即崩溃（零 fallback）
- **独立策略多 track**: `strategy_config` 表允许多策略并行运行，各自独立资金核算

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
| `docs/DATA_DICTIONARY.md` | 数据字典 (market.db / trades.db 全表全字段) |
| `docs/research/A股有效因子普查_2026-07-07.md` | 因子普查汇总 (12家券商研报) |
| `docs/research/因子失效记录_2026-07-07.md` | 24因子失效记录与原因 |
| `docs/research/四因子接入分析_2026-07-07.md` | OIR/STR/ABN_TURN/OCFP 接入分析 |
| `docs/research/oir-factor-deep-dive.md` | OIR 计算细节 |
| `docs/research/str-factor-deep-dive.md` | STR 计算细节 |
| `docs/research/abn-turnover-factor-deep-dive.md` | ABN_TURN 计算细节 |
| `docs/research/ocfp-factor-deep-dive.md` | OCFP 计算细节 |
| `docs/research/A股量化因子全量研究报告_2026-07-07.md` | 涨跌停制度特有效因子 (50+因子) |
| `docs/research/涨跌停因子接入分析_2026-07-07.md` | P71 四因子接入分析 |
| `docs/research/数据源适配因子清单_2026-07-07.md` | P72 数据源适配 — 55因子, 3个落地 |
| `docs/research/量化因子回测策略业界标准_2026-07-07.md` | 业界标准 — 5阶段流程(CPCV+walk-forward+PBO) |
| `docs/research/回测策略业界标准分析_2026-07-07.md` | 标准分析 + 与当前流程对比 |
| `docs/adr/025-launchd-keepalive-policy.md` | ADR 025: KeepAlive 策略 — scheduler ✅ / webapp ❌ |

## Data quirks (not bugs)

### Cash balance ≠ initial_capital - Σ(stock_cost)
差额是交易成本：佣金(万三，最低¥5/笔) + 滑点(千一，双向)。CostModel 在 `execution/cost.py`。
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
