# CLAUDE.md

A股量化选股系统。Grinold & Kahn 7 层架构：数据 → 因子 → Alpha → 风控 → 优化 → 执行 → 监控。¥5,000 起步。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521) — 用户执行
bash scripts/restart.sh

# 因子缓存物化 (手动指定日期)
PYTHONPATH=. .venv/bin/python -c "from quant.scheduler.factor_cache import _run; _run('2020-01-01')"

# LightGBM 训练
PYTHONPATH=. .venv/bin/python -c "from quant.alpha.qlib_model import train_lgb_model; train_lgb_model()"

# 回测
PYTHONPATH=. .venv/bin/python -c "from quant.backtest.loop import run_backtest; r=run_backtest('2024-01-01','2025-12-31',capital=5000); print(r['metrics'])"

# 测试 — 必须用 .venv (optuna/hmmlearn 在 .venv 中)
PYTHONPATH=. .venv/bin/python -m pytest test/ -v
```

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

- optuna / hmmlearn 在 `.venv` 中，测试和脚本须用 `.venv/bin/python`
- 因子缓存 gzip CSV ≈ 1.5GB / 6年，无需裁剪（`factor_cache_max_days: 2000`）
- `factor_fail_fast=False` — 单因子失败不阻断整批物化
