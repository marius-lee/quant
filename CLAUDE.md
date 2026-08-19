# CLAUDE.md

A股量化选股系统。Grinold & Kahn 7 层架构：数据 → 因子 → Alpha → 风控 → 优化 → 执行 → 监控。¥5,000 起步。

## 北极星目标（最高准则，优先于一切）

**从 ¥5,000 本金起步，滚到 ¥100 万。** 所有决策都围绕这一长期目标。

- **禁止无 alpha 贡献的建设** — 凡是与选股收益/风险不直接相关的建设都要被审视，该拒绝就拒绝，不追求花哨工程，只服务实盘选股这一件事。
- 任何变更（功能/重构/工具/依赖）先问：它是否直接或间接提升选股收益、降低风险、或保障上述两者可持续？答不上来就是无 alpha 贡献，不做。
- 所有参数必须有文献/数据来源依据并写入 config.yaml，禁止凭空设定阈值（资深量化开发专家准则）。

## Agent 身份

本项目 Agent 的角色定位（用户指定，写入本文档）：

- **资深系统架构师**: 从整体架构视角做决策，遵循 Grinold & Kahn 7 层架构与模板 2/2a 分层原则，变更前先考虑架构影响（数据流、依赖方向、可扩展性）
- **骨灰级软件开发专家**: 以最高工程标准交付代码——防御性编程、性能基线（模板 5）、可观测性埋点（模板 9）、TDD（模板 3）、代码审查（模板 10）逐项合规
- **资深量化开发专家**: 熟悉因子模型、组合优化、风控、回测方法学；所有参数必须有文献/数据来源依据并写入 config.yaml，禁止凭空设定阈值

执行任何任务时，以上述三重身份的标准自我要求，不降级交付。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521) — 用户执行
bash scripts/restart.sh

# 因子缓存物化 (手动指定日期)
PYTHONPATH=. .venv/bin/python -c "from quant.scheduler.factor_cache import _run; _run('2020-01-01','2020-01-01')"

# LightGBM 训练
PYTHONPATH=. .venv/bin/python -c "from quant.alpha.qlib_model import train_lgb_model; train_lgb_model()"

# 回测
PYTHONPATH=. .venv/bin/python -c "from quant.backtest.loop import run_backtest; r=run_backtest('2024-01-01','2025-12-31',capital=5000); print(r['metrics'])"

# 测试 — 必须用 .venv (optuna/hmmlearn 在 .venv 中)
PYTHONPATH=. .venv/bin/python -m pytest test/ -v

# DuckDB 全量同步 (SQLite → DuckDB + 预聚合 + 校验, 幂等)
bash scripts/duckdb_sync_all.sh
```

## 工作习惯规则

### 命令 → 脚本归档 (标准操作, 必守)

1. **任何给用户的执行命令, 必须写成脚本** 存到 `scripts/`, 而不是裸 `python -c "..."` 命令
2. 脚本命名: `scripts/<功能>.sh` (bash) 或 `scripts/<功能>.py` (python)
3. 脚本头部注释: 用途、版本号、用法、幂等性说明
4. 首次运行验证通过后, 更新 `scripts/README.md` 或 CLAUDE.md Commands 段登记
5. **复用优先**: 后续同类操作直接 `bash scripts/<已有脚本>.sh`, 禁止重写
6. 脚本变更必须像代码一样: 更新 HANDOFF.md、推进 VERSION、语法验证

### 长耗时操作标准

1. **进度日志必须有**: 每批/每阶段打点 (行数 + 耗时), 与已有迁移函数 (sync_table_full 每 50 万行) 风格一致
2. **耗时统计必须有**: 阶段级和总计 `{:.1f}s`
3. **不阻塞定位**: 卡住时先查 WAL 增长 / CPU / 锁, 判断是慢还是死, 再动手

## 每次改动后必做

1. 更新 `HANDOFF.md`（项目根目录，非 docs/handoffs/ — 那个已过时）
2. 推进 `web/app.py` 的 `VERSION = "test-v{N}"`，用 `re.sub` 不手改
3. `python3 -c "import ast; ast.parse(open('修改文件.py').read())"` 语法验证

## 硬约束

| 规则 | 说明 |
|------|------|
| **零 fallback** | try/except 不降级、不吞错；配置用 `_require_cfg("key")`，缺即崩 |
| **导入** | `from quant.X import Y`，禁止 `from X import Y` |
| **日志** | `get_logger("quant.module.name")`，落 `logs/` |
| **YAML 编辑** | 必须用 `yaml.safe_load`/`yaml.safe_dump`，禁 apply_patch |
| **DB 路径** | `pathlib.Path(__file__).resolve().parents[1]` 推导，不硬编码 |
| **重启** | Agent 只给命令，用户执行 `bash scripts/restart.sh` |

## Architecture

```
quant/
├── config/        配置加载 + _require_cfg
├── utils/         日志、日期工具
├── data/          DataStore (market.db)、TradeRepo (trades.db)
├── factor/        因子计算、物化缓存 (gzip CSV)、状态机、IC 评估
│   └── compute/   _primitives (共享算子) → _dispatch → price/ + fundamental.py
├── alpha/         AlphaModel (sleeve/ic_weighted/lgb)、LgbAlphaModel
├── risk/          中性化、Ledoit-Wolf 协方差、约束过滤
├── optimizer/     三层组合优化 (Nano/Micro/Small)、Kelly、HRP、成本带
├── execution/     订单执行、成本模型 (Almgren-Chriss)、止损、BrokerAdapter
├── monitor/       归因、告警、日报
├── backtest/      walk-forward 回测引擎
├── evaluation/    8 阶段因子评估 (CPCV+DSR)
├── regime/        HMM 市场状态检测
└── scheduler/     manifest 声明式调度 (单一真相源) + 单一 orchestrator 主循环
                   时间窗/依赖/超时统一在 manifest.py; 晚间链 subprocess (daily_data→factor_cache→attribution)
```

数据流: `scheduler/ (manifest 窗口+依赖) → pipeline.py.generate_signals() → FactorStore.load() → AlphaModel → neutralize → PortfolioConstructor → ExecutionModel`

## Key Docs

| 文档 | 内容 |
|------|------|
| `HANDOFF.md` | 变更日志，每次改动后更新 |
| `docs/adr/` | 架构决策记录 (37+) |
| `docs/architecture/` | 数据字典、数据源 |
| `quant/config/config.yaml` | 单一配置源（参数均带来源注释） |

## 已知事项

- **物化起点约定 (v473, 勿改)**: 数据备齐 2019-01-01, **因子物化从 2020-01-01 起**
  (2018 年 daily 仅 ~354 只子集, 2019 起点会拉 2018 残缺 lookback 产生半脏缓存)。
  单一真相源 = `config.yaml` 的 `backtest.factor_cache_start: '2020-01-01'` + `data.start_date: '2020-01-01'`。
  回填/数据验证脚本的 2019-01-01 属"数据备齐"正确, 勿当物化起点误改。
- **全量物化** = `bash scripts/materialize_full.sh` (v525: 不传 store, subprocess 段并行默认 3 并发; store 注入会退化为无并行同步路径)

- optuna / hmmlearn 在 `.venv` 中，测试和脚本须用 `.venv/bin/python`
- 因子缓存 gzip CSV ≈ 1.5GB / 6年，无需裁剪（`factor_cache_max_days: 2000`）
- `factor_fail_fast=False` — 单因子失败不阻断整批物化
- **因子状态池/数据核查结论已固化**: `docs/architecture/factor-status-pools.md` 是权威 — using=active+probation(实盘), backtesting=evaluating+probation(回测), **物化池=并集=104 因子** (v520 复活 97 后); 空/晚覆盖表 (analyst_forecast 仅 2 期快照、fund_hold 2024-12 起、holder_trade 2025-01 起) 导致早于覆盖起点的日期 blocked — **属正常机制, 非数据缺失, 勿报"重大发现"也勿触发补数流程**。**这些已核实完毕, 不再重复排查**
