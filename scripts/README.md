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
| `materialize_full.sh` | 全量重建因子缓存 (force=True, 整段覆盖) |
| `materialize_range.sh` | 按日期区间补齐因子缓存 (幂等, 只补缺失) |
| `rematerialize_industry_pit.sh` | 行业 PIT 生效后重物化 2020 起因子缓存 (v502) |
| `rebuild_factor_cache.py` | 重建 factor_cache.json |

## 行业 PIT (v502)

| 脚本 | 用途 |
|------|------|
| `sync_industry_history.sh [batch]` | 同步 baostock 行业 PIT 历史 → industry_history 表 (幂等断点续跑) |
| `resume_industry_sync.sh [max_retries]` | 同步守护续跑 — 网络中断 (10002007) 自动重启, 断点续跑; 达 5 万/日上限自动进入等待模式每 30s 探测公网 IP, 换热点自动清零续跑 (v508+v513+v518) |
| `reset_baostock_day.sh` | 兜底: 手动清零 baostock 日计数 + 解除黑名单 (v513; 通常换热点后自动处理, 无需手动) |
| `notify_test.sh [--no-macos] [--title T]` | 通知通道连通测试 (macOS 弹窗+提示音 / Server酱 / Telegram / 企微, 幂等) (v515) |
| `mark_industry_skip.py [--dry-run]` | 行业数据源缺失标记 — 北交所 920 段 + <30 天次新 → skip 表, 同步剔除 (幂等) (v516) |
| `industry_pit_activate.sh [--skip-wait]` | 一键顺序链: 等后台同步完成 → 校验 → 重物化 (v502) |
| `verify_industry_pit.sh` | 校验 industry_history 覆盖 + smoke 回测验证 PIT 中性化不崩 |
| `rematerialize_industry_pit.sh` | 同步完成后重物化 2020 起因子缓存 (行业 PIT 生效) |

顺序: `industry_pit_activate.sh` 一键执行 (等同步 → 校验 → 重物化) → `restart.sh` 重启。

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
