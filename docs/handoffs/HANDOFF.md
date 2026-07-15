# HANDOFF — 2026-07-15 ~22:00 CST

## 当前状态：日志修复 + 冒烟测试优化 + IC 计算提速 (test-v84)

### #49: 日志系统全面修复 (13 files)
- JSON 统一格式、stderr 过滤、propagate=False、双前缀修复、trace_id 提前
- trace_id 不重复设置 (pipeline.py: get_trace_id() or new)

### #50: 冒烟测试 — 因子范围修正
- `factor_status_filter="backtesting"` → `"active"` (66→2 因子)
- 耗时从数十分钟回到 151.6s
- **注意**: 冒烟测试用 active，诊断模块必须用 backtesting——两者职责不同

### #51: IC 计算提速 — primitives 延伸

**根因**: `compute_ic()` 虽然一次性加载了 data，但没调 `precompute_primitives`，导致每个交易日 `compute_all_factors` 都从原始行情重算 sma_20/vol_20/ret_5d 等滚动窗口。66 因子 × 60 天 = 3960 次重复计算。

`run_backtest()` → `generate_signals()` 链路早就在用 primitives，只是 `compute_ic()` 忘了传。

**修复**: `quant/factor/ic.py` — 3 处改动:
1. 加载 data 后调 `precompute_primitives(data)` 一次
2. `_compute_one_day` 内按日期切片 primitives
3. 切片后的 prims 传给 `compute_all_factors(primitives=ds_prims)`

效果: IC 计算阶段提速 ~3-5x（12 个 shortcut 因子走 O(1) 快捷路径，其余因子也复用滚动窗口中间值）。

### 版本: test-v84

### #53: 僵尸任务三层防护 (test-v84)

**修复内容**:
1. `execute.py` 末尾补 `_tk_finish("ok")` — 之前只打日志不写 DB
2. `task_log.start()` 插入前自动 abort 同任务同日期旧 running 行
3. `orchestrator.py` 新增 `_check_timeouts()` — 每轮 poll 扫描超时 running, 任务专属阈值
4. `api_scheduler()` 超时显示同步改为任务专属阈值

**各任务超时阈值**:
| 任务 | 阈值 | 正常耗时 |
|------|:---:|------|
| signals | 15min | ~5min |
| execute | 10min | <1min |
| monitor | 不收市不检查 | 09:35-14:55 |
| attribution | 15min | ~3min |
| weekly_eval | 120min | ~30min |

**待归档: heartbeat 方案** — `task_runs` 加 `last_heartbeat` 列, 长任务关键循环点心跳更新, watchdog 查 DB 替代固定阈值. 当前固定阈值方案够用, heartbeat 留待将来.

### #54: Phase 2 primitives 预计算优化 (test-v84)

**问题**: `compute_factor_stats()` 的 `_thread_compute_chunk` 逐因子逐日调用 `compute_all_factors()`, primitives 重复计算 N 次
(与回测诊断 4.7h→8min 同根问题).

**方案**: 主线程统一预计算 primitives, 线程 worker 复用.
- `compute_factor_stats` Phase B 之前新增 primitives 预计算块
- `_thread_compute_chunk` 接受 `shared_data` + `shared_primitives` + `shared_financials` 参数
- worker 内调用 `compute_all_factors(data, date_str, precomputed_primitives=prims)`
- 去掉 worker 里独立打开 DataStore / 重复加载数据的逻辑

**改动文件**: `quant/factor/stats_cache.py`
**影响**: Phase 2 单因子 IC 评估耗时预计降低 60-80%
