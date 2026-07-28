# Scripts

运维脚本和测试工具。

## 日常运维

| 脚本 | 用途 |
|------|------|
| `restart.sh` | 重启 Web 服务 |
| `run_task.sh` / `run_task.py` | 手动触发调度任务 (signals/execute/monitor/attribution/daily_data/factor_cache/weekly) |
| `setup_cron.sh` | 安装 crontab 定时调度 |
| `init_data.py` | 数据源初始化 (全量/行业/日线/基本面/基准指数) |

## 因子评估

```bash
bash scripts/eval_layer12.sh       # L1+L2 快速评估
bash scripts/eval_stepwise.sh      # L1+L2+L3 完整评估
bash scripts/eval_standard.sh      # 五阶段标准评估 (CPCV+walk-forward+PBO)
```

## 因子管理

| 脚本 | 用途 |
|------|------|
| `reset_eval.sh` | 重置评估状态 |
| `reset_rejected.sh` | 重置 rejected → retired |
| `generate_factor_cards.py` | 生成因子卡片 JSON |
| `materialize_factors.py` | 全量物化因子值到 factor_cache.db |
| `rebuild_factor_cache.py` | 重建 factor_cache.json |

## 测试

| 脚本 | 用途 |
|------|------|
| `smoke_v252.py` | 当前版本冒烟测试 |

## 用法

```bash
cd /Users/mariusto/project/quant
PYTHONPATH=. bash scripts/run_task.sh signals 2026-07-23
PYTHONPATH=. .venv/bin/python3 scripts/smoke_v252.py
```
