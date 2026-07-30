# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。


## 当前状态 (test-v284, 2026-07-30)

### test-v284: attribution 独立三档归因

资金分档 (独立于 optimizer, 纯归因维度):
  Nano  (精简): AUM < ¥50,000
  Micro (标准): ¥50,000 ≤ AUM ≤ ¥500,000
  Small (严格): AUM > ¥500,000

归因模块行为矩阵:

| 模块        | 功能              | Nano (精简)     | Micro (标准)     | Small (严格)     |
|------------|-------------------|:---------------:|:----------------:|:----------------:|
| Brinson    | 行业配置/选股归因   | 跳过              | 正常              | 正常              |
| G1 OOS     | Walk-Forward 验证  | 正常              | 正常              | 正常              |
| L1-L3      | IC衰减/状态变更     | 正常              | 正常              | 正常              |
| 因子冗余    | IC-rank 相关性去重  | 正常              | 正常              | 正常              |
| G2 拥挤度   | pairwise ρ 检测    | 正常              | 正常              | 正常              |
| G3 DSR     | Deflated Sharpe   | 跳过              | 正常 (>=10d)      | 正常 (>=20d)      |
| G4 因子PnL | 因子贡献 bps       | 正常              | 正常              | 正常              |
| R3 换手率   | turnover vs alpha | 不限 (999%阈值)   | 告警 >=200%       | 告警 >=50%        |
| R4 信号滑点 | signal->execution | 告警 >=5%         | 告警 >=2%         | 告警 >=1%         |
| IC 同步    | 写 factor_registry| 正常              | 正常              | 正常              |
| Benchmark  | 净值追踪           | 正常              | 正常              | 正常              |

设计依据:
  - Brinson 需要 >=5 行业 + >=20 只票 (Barra), <50K 组合无统计意义
  - DSR 需要 >=60 交易日 (De Prado 2018), <50K 组合数据不足
  - 换手率小额一笔翻倍, 告警无意义 (Axioma 按 AUM 分档)
  - 信号滑点在小额=隔夜跳空, 非执行问题 (Kissell IS)
  - 因子健康检测是核心风控, 不分资金规模

自动升降级:
  - AUM 实时读取 ExecutionEngine.get_capital(), 不缓存
  - 跨过阈值自动切换, 下次 _run 生效

### test-v283→v282: config 来源注释恢复

- test-v249 (07-29) 误删 67 行参数来源对照表.
  恢复头部注释 + 底部 50 行参数来源表 (Barra/Kissell/Grinold/De Prado).
- 所有新增参数均带来源注释 (train_chunk_samples, tier caps 等).

### test-v281: PID 进程死活检测

- _cleanup_zombie_tasks: 原全部 running->aborted (不查进程死活).
  改 os.kill(pid,0) 检测, 进程存活→保留 running, 真死→aborted.
- task_runs 表加 pid 列, _tk_start 写入 os.getpid().
- orchestrator 不重试 aborted→恢复为重试 (aborted 现在=真死, 应重试).

### test-v280: 界面调度时间改依赖触发

- status.py register_all: factor_cache/attribution/lgb_train 的 schedule
  从固定时间改为 "daily_data完成后" / "factor_cache完成后".

### test-v278: _next_scheduled_time 依赖型兼容

- 依赖型任务 (schedule 含 "完成后") 返回空串, 不计算 next_run.

### test-v276→v277: LGB 全链路 float32 + 分块训练

- X_day/y_day 构建时 .astype(np.float32), factor_panels 转 float32.
- train() 分块训练: 每批 <=4M 样本, init_model 串联.
- 内存峰值 ~25GB -> ~4GB.
- config 加 train_chunk_samples (来源: 2026-07-30 OOM 实测).

### test-v274: factor_cache 整块跳过 + turnover 单日回填

- factor_cache: chunk 前检查全部日期已缓存→跳过 (force 除外).
- backfill_turnover: date=today 单日模式, 不再扫全历史缺口.

### test-v272: baostock OHLCV + 数据源链重排

- 新增 _fetch_baostock_daily (qfq 前复权, turnover✅).
- 源顺序: tushare -> zzshare -> pytdx -> baostock -> tencent -> akshare -> tickflow -> longbridge.

### test-v271: _analyze_daily_gaps 过滤非 stocks 表 symbol

- ETF/退市股在 daily 有历史残留, for 循环加 if sym not in all_symbols: continue.

### test-v270: update_daily 加 target_date 精准补数据

- _analyze_daily_gaps 以 target_date 为 staleness 基准.
- 补单日数据只拉真正缺口 (507 只 vs 5481).

### test-v269: sync_adj_factor 接入 baostock 兜底

- tushare adj_factor 限流 ~3-4 批/天, 改为 baostock query_adjust_factor 兜底.
- baostock 25 分钟铺满 4874 只 (88%).

### 版本号
web/app.py VERSION = "test-v284"

### 待完成
1. 晚间链 (evening chain) 完整跑通 07-30
2. 全量回测
3. git push


## 历史归档

### [已归档] test-v275 及更早版本

见 git log 和旧版 HANDOFF.md.
