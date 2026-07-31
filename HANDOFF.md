# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。


## 当前状态 (test-v306, 2026-07-31)

### 最终状态

git: 34 commits (v269→v306), clean working tree
VERSION: test-v306
cron: 1条 (adj_factor 每天19:50)
orchestrator: ~85MB, 16个调度任务

### test-v304→v306

- v304: 调度序号列
- v305: adj_factor 每天一次 (baostock兜底), 显示优化
- v306: phase5 probation因子走IC_PERSISTENT, 周度评估正式运行(0active/26probation/45archived)

### 待观察
- 限价单盘中成交
- 晚间链 subprocess 无人值守首次运行
- 周一信号 (周六不交易)



### 完整架构



### 16 个调度任务

| 任务 | 调度 | 类型 |
|------|------|------|
| signals | 08:30 | orchestrator |
| execute | 09:30 | orchestrator |
| monitor | 09:35-11:30,13:00-14:55 | orchestrator |
| reconcile | 15:05 | orchestrator |
| daily_data | 19:00 | orchestrator subprocess |
| factor_cache | daily_data完成后 | orchestrator subprocess |
| attribution | factor_cache完成后 | orchestrator subprocess |
| lgb_train | 周一/周四 factor_cache完成后 | orchestrator subprocess |
| adj_factor | 每小时 :50 | cron |
| factor_curation | 周六 06:00 | orchestrator subprocess |
| eval_phase1 | 周六 06:00 | orchestrator subprocess |
| eval_phase2 | 周六 06:00 | orchestrator subprocess |
| eval_phase3 | 周六 06:00 | orchestrator subprocess |
| eval_phase4 | 周六 06:00 | orchestrator subprocess |
| eval_phase5 | 周六 06:00 | orchestrator subprocess |
| weekly_eval | 周六 06:00 | orchestrator subprocess |

### test-v303 (当前)

- _next_scheduled_time int(x or 0): 兼容空小时格式 (每小时 :50)

### test-v300→v302: 周度评估全自动

- 五阶段评估接入 weekly.py: Phase1(数据)→Phase2(IC/ICIR)→Phase3(CPCV/PBO)→Phase4(成本)→Phase5(状态同步)
- 每周六 06:00 orchestrator subprocess 自动触发
- eval_standard.sh 不再需要手动运行
- adj_factor 注册为界面任务
- cron 清理: 移除 signals/execute/monitor/reconcile/weekly (orchestrator 已接管)

### test-v297→v299: numpy 2.x ufunc 全量修复

- np.log/exp/isfinite 在 Python float 上崩溃 (numpy 2.x breaking change)
- 修复 6 处: size_neutralize, ctr_20d, _alternative(mv_map), _dispatch(earnings), _alternative(ideal_amplitude)

### 版本号
web/app.py VERSION = "test-v303"

### 运行中
- 回测 (run_backtest 2024-2025, 已修 numpy bug, 预计 1-2h)

## 历史归档

### [已归档] test-v275 及更早版本

见 git log 和旧版 HANDOFF.md.
