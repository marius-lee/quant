# 周六周度因子评估调度断链 — 根因分析 & 修复归档 (v417, 2026-08-08)

## 1. 现象

- Web 调度页「因子评估(总)」(weekly_eval, 注册显示 `周六 06:00`) **从未启动**
- 96 个已注册因子全部停留在 `evaluating`/`probation`/`archived` 状态，`factor_registry` 无任何因子晋升 `active` — 每周六 06:00 的五阶段评估 (策展→数据→IC→CPCV→成本→状态同步) 从未实际执行, 因子池永远不会向上演化。
- 手动执行 `bash scripts/run_task.sh weekly` 可正常跑通 → 任务本身健康, 断的是**调度触发**。

## 2. 根因 — 三路触发源全部失效

系统设计上 weekly_eval 有 3 条彼此独立的触发路径 (互为冗余), 而 3 条全部断掉:

| 触发源 | 设计位置 | 实际状态 | 失效原因 |
|---|---|---|---|
| ① orchestrator 周六分支 | `orchestrator.py` 主循环内, v301 (2026-07-31) 引入 | **永不可达** | 触发块位于 `if not is_trading_day(): continue` **之后** — 周六不是交易日 → 主循环每次轮询都直接从顶部 `continue`, 循环体 (含触发块) 从未执行 |
| ② 独立 weekly 线程 | `scheduler/__init__.py::start_all()` → `_weekly_loop` (周六 06:00, 专为周末设计, 不检查交易日) | **死代码** | `scripts/restart.sh` / `run_task.sh daemon` 一直只调 `orchestrator.start()`, `start_all()` 零调用者 → 线程从未被拉起 |
| ③ cron 兜底 | `scripts/setup_cron.sh` 声明 `0 6 * * 6 ... run_task.sh weekly` | **实际为空** | `crontab -l` 只有注释行、无任何任务条目; 而 `.cron_installed` 标记文件 (7月16 日残留) 让 Web `/api/scheduler` 误显示 cron "已配置" |

**附带缺陷**:
- `orchestrator._TIMEOUTS` 无 `weekly_eval` — 评估 subprocess 若卡死会永久占用 `task_runs.status='running'`, 之后任何触发都会被 `_tk_start` dedup 挡住, 调度永久阻塞且无日志告警。
- 触窗口 `06:00-06:05` 只有 5 分钟 — 周六上午 restart 错过这一分钟级窗口, 整周评估即丢失。

## 3. 修复 (v417)

1. `quant/scheduler/orchestrator.py`
   - 状态读取 (`_get_today_status` / `_get_today_aborted` / `_retry_ok`) **前移**到循环顶部。
   - weekly_eval 触发块**前移**到 `if not is_trading_day(): continue` **之前** — 周六非交易日也照常触发。
   - 触发窗口 `06:00-06:05` → `06:00-12:00` (周六上午 restart 仍可补跑; 三路触发同时命中时由 `_tk_start` dedup 保证只跑一次)。
   - 删除原位于晚间链前的旧 (不可达) 触发块。
   - `_TIMEOUTS["weekly_eval"] = 43200` (12h 僵尸超时)。
2. `scripts/restart.sh` / `scripts/run_task.sh daemon`
   - 入口 `from quant.scheduler.orchestrator import start` → `from quant.scheduler import start_all` — 单进程双线程: orchestrator + `_weekly_loop`。pkill 匹配串同步更新。
3. `scripts/setup_cron.sh`
   - 重写为只装两条: `0 6 * * 6 weekly` + `50 * * * * adj_factor` (日频任务归 orchestrator, 不进 cron 双跑)。
   - 修复 heredoc 变量未展开 bug: macOS bash 3.2 对**双引号** heredoc (<< "X") 不展开 `$PROJ` — 原脚本写入 crontab 的 `cd $PROJ && ...` 在 crond 中 `$PROJ` 为空 → 实际执行 `cd ` 空路径。改用**无引号** heredoc 已实测展开。
   - 重新执行脚本 → `crontab -l` 验证两条任务含完整路径。
4. `quant/scheduler/weekly.py`: `grace_seconds 7200 → 43200`,与 `_TIMEOUTS` 对齐 (防 dedup 误 abort 活任务)。
5. 可观测性: `status.py::_next_scheduled_time` 已原生支持 "周六 HH:MM" 格式 → Web `/api/scheduler` 自下周六起正确显示 `next_run`; orchestrator 周六窗口内打诊断日志。

## 4. 验证

- 新增 `test/test_weekly_sat_trigger_v416.py` **6/6**:
  1. weekly_eval 触发块在源码中的位置 < `if not is_trading_day():` 位置 (控制流回归, 防再次前移回退)
  2. 触发块在 `def _run` 的 while True 循环内
  3. 窗口上界 = 12:00
  4. `start_all()` 同时拉起 orchestrator + weekly 两线
  5. `restart.sh` 走 `start_all` 入口
  6. `setup_cron.sh` 含 weekly + adj_factor 条目
- 全量回归: **263 passed** (v416 的 257 + 新增 6)。
- crontab 实测: 两条任务含完整展开路径。

## 5. 验证点 & 待办

- **2026-08-15 (周六) 06:00**: 首次真实自动执行验证 — 三路触发源任一路命中即跑; 检查 `task_runs` 出现 `weekly_eval` 记录, 因子状态推进。
- Web `/api/scheduler` 下周出现 `next_run`。
- 若 8-15 未触发: 先 `bash scripts/run_task.sh weekly` 手动兜底, 再查三段: orchestrator 是否常驻 (logs/orchestrator.log), weekly 线程是否拉起, crontab 是否含两条。

## 6. 关联历史

- v301 (2026-07-31): cron 清理 (仅留 adj_factor) + orchestrator 加周六评估 — **本 bug 引入 commit** (触发块位置错误 + 入口未切 start_all + cron 未重灌 三重落空)。
- ADR-042: 拒绝纳入名单中包含与本文相关的调度治理决策。
- 未修复其他: 无。