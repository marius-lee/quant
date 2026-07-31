# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。


## 当前状态 (test-v299, 2026-07-31)

### test-v297→v299: numpy 2.x ufunc 兼容全量修复

- np.log/exp/isfinite 在 Python float 上崩溃 (numpy 2.x breaking change).
  修复 6 处: size_neutralize, ctr_20d, _alternative(mv_map), _dispatch(earnings).
  改法统一: np.asarray(..., dtype=np.float64) 或 pd.to_numeric.

### test-v296: 因子评估调度改为依赖显示

- weekly_eval schedule: 周六06:00 → factor_curation完成后.
  与晚间链(daily_data完成后→factor_cache完成后→attribution)统一风格.

### test-v293→v295: 调度界面优化

- 新增说明列(desc), 移到最后一列. 去掉分组+Cron列.
- 前5列强制 nowrap. 括号注释兼容.

### test-v288→v291: orchestrator 瘦身 + 晚间链 subprocess

- orchestrator 只负责 08:30-15:05 (signals→execute→monitor→reconcile).
- 晚间链由 orchestrator 在 19:00 通过 subprocess.Popen 触发, 非阻塞轮询.
- 失败自动重试 (最多2次). cron 移除 evening 条目.
- orchestrator 从 919MB → 85MB (不 import sklearn/scipy).
- lgb_train 补回晚间链 (周一/周四执行).

### test-v284→v287: attribution 三档 + config 重建

- Nano(<50K)/Micro(50-500K)/Small(>500K) 三档归因.
- 换手率/滑点告警按资金分档.
- config.yaml 恢复 67 行参数来源对照表 (test-v249 误删).

### test-v281→v282: PID 进程检测

- task_runs 加 pid 列, _tk_start 写入 os.getpid().
- _cleanup_zombie_tasks: os.kill(pid,0) 检测进程死活.
- 活进程保留 running, 真死 → aborted → orchestrator 重试.

### test-v276→v280: LGB 全链路 float32 + 分块训练

- 因子面板/X_day/y_day 全转 float32. 分块训练(每批4M).
- 内存 ~25GB → ~4GB. OOM 解决.

### test-v274: factor_cache chunk skip + turnover 单日回填

- chunk 跳过: 检查文件存在(不检查因子完整性).
- backfill_turnover(date=today) 单日模式.

### test-v269→v272: adj_factor baostock + 数据源链重排 + baostock OHLCV

- sync_adj_factor 双源: tushare(批量) + baostock(兜底).
- 源链: tushare→zzshare→pytdx→baostock→tencent→akshare→tickflow→longbridge.
- ETF 过滤 + target_date 精准补数据.

### 版本号
web/app.py VERSION = "test-v299"

### 待完成
1. 回测结果 (07-30→07-31 已修 numpy bug, 正在跑)
2. 验证晚间链 subprocess + factor_cache chunk skip 联合效果

## 历史归档

### [已归档] test-v275 及更早版本

见 git log 和旧版 HANDOFF.md.
