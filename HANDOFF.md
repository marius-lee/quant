# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `grep -rn "关键词" HANDOFF.md docs/adr/` 联动搜索，避免重复踩坑。

## 当前状态 (test-v356, 2026-08-02)

### 关键指标
- 因子: 84 注册 (0 active, 20 evaluating, 25 probation, 46 archived — 8 个新注册待评估)
- 数据: 2019-2026 日线, 5208 只 (2019: 3551 只)
- adj_factor: 4924/5208 覆盖 (94.5%), 2020-01-02 起
- 因子缓存: 物化性能优化 A/B/C2 落地; C1(parquet 列存) 保留设计, 待评估后实施
- 回测: 冒烟通过 (CAGR=-23.5%, 0 errors, avg 2.2 信号/天), 全量待跑
- scheduler: orchestrator (16 tasks, ~85MB) + cron (清空)
- 测试: validate_factors.py 100/100 通过; pytest factor 相关 47/47 通过

### 晚间链流程
```
19:00 daily_data → adj_factor → factor_cache → attribution → lgb_train(Mon/Thu)
```

### test-v310→v355 变更总览

**v357**: ADR-043 layer1 — 10因子aux覆盖, 消除per-date DB泄漏
- A1: alpha035 `rolling.apply` → numpy 向量化 ts_rank, 单因子 4.8s/日 → 0.4s/整块
- A2: shortcut 因子预计算整块 zscore panel, 物化时直接取行
- A3: CSV 由逐日 gzip 解压-压缩改为 chunk 级批量合并写
- A4: precompute_primitives 按需计算窗口, 非全量 183 张表
- B1: chunk 内逐日计算支持 ProcessPoolExecutor(max_workers=4)
- B2: 基本面 PIT 由逐日循环改为向量化 panel (pivot + rolling)
- C2: 新增日期级 manifest.json, is_materialized 读清单而非扫描文件
- 新增脚本 `scripts/benchmark_factor_cache.py`: 用临时缓存目录跑 chunk 1 基准, 不污染生产缓存, 输出全量 9 chunks 估算
- 新增脚本 `scripts/run_factor_cache_chunk1.py`: 清空生产缓存并正式跑 chunk 1 (2019-06-03 -> 2020-03-26), 输出全量估算
- C1: **parquet 列存 — 经分析后决定不实施**
  - 基准 (22 factor × 5208 symbol / 日): gzip CSV 784 KB, parquet snappy 1266 KB (+61%), parquet f32+brotli 759 KB (-3%)
  - 写速度 parquet 快 9x, 但物化瓶颈仍是 compute (小时级), 写仅占分钟级
  - 读场景: pipeline 用 `factor_names=None` 全读, 条件读 pushdown 用不上
  - 迁移成本高 (1.8GB 现存缓存 + 双格式兼容), gzip CSV 零依赖、易调试
  - 结论: 当前 ROI 低, 保留为候选; 未来因子 >300 或 pipeline 改为部分因子读取时再评估
**v355**: (已并入 v356 统一归档)
**v310-v312**: 界面显示市场状态 + hmmlearn 依赖修复
**v313**: ATR 峰值持久化 position_meta
**v314**: 消除全部 except:pass (30+ 处)
**v315**: ADR-042 归档被拒绝的架构/算法建议 (24 项 + 3 决策原则)
**v316**: adj_factor 从 cron 迁入 orchestrator → 回退 (晚间链原则)
**v317**: adj_factor 迁入晚间链子进程, cron 清空
**v318**: fix _next_scheduled_time 非时间格式崩溃
**v319**: 调度描述链式依赖顺序修正
**v320**: 对账正常=绿色 异常=红色 (A 股红涨绿跌)
**v321**: 日志定期清理 (启动 7 天 + weekly 14 天)
**v322**: factor_curator 修复 — 日期 Timestamp/索引重复/source (22→4 注册)
**v323**: 4 核心缺失因子 (market_beta_60d, overnight_gap_5d, vol_price_sync_20d, revenue_growth_yoy)
**v324**: 日内反转因子 + 9:30 快照表 + snapshot 任务
**v325**: 成长因子 (earnings_growth_yoy) + Piotroski F-Score (9 项质量打分)
**v326**: Alpha101 7 个最高优先级因子
**v327**: 快照加成交量 + 开盘成交量占比因子
**v328**: 尾盘快照 (14:55) + 尾盘异动因子
**v329-v330**: 状态栏加日期+周几
**v331**: fix phase5 f_repo 未定义 + 手动补跑周评 (8 因子注册)
**v332**: 状态池重构 — 废弃 using/backtesting 间接层 → get_signal_factors/get_evaluable_factors
**v333**: curator 抑制 spearmanr ConstantInputWarning
**v334**: fix market_beta_60d common_dates 顺序 + 因子审计 (28 问题/1 真 Bug)
**v335**: 因子计算全面加固 — 11 raise→return None + trcf/fund_flow/ztd
**v336**: 重做: 13 raise→return None + ztd numpy + trcf/fund_flow
**v337**: Bug A — reversal_5d 改为真反转 (-cum)
**v338**: Bug B+D+E+G — beta 日期对齐/量价改收益率/板块涨停/vp_divergence
**v339**: Bug E 涨停分板块 (科创 20%/北交 30%)
**v340**: Bug C 删 turnover fallback + Bug F MIF 文档修正 + Bug A 注释清理
**v341-v343**: Python 3.14 broker 作用域修复
**v344**: P0 — pipeline broker suppress_push 保护 + 回测失败不持久化
**v345**: P1 — np.log 类型/状态机事件/Phase2+5 术语对齐
**v346**: Phase2+5 输出完全对齐状态机四态 (active/probation/archived)
**v347**: fix np.log — pd.to_numeric→astype(float) (DataFrame 兼容)
**v348**: fix update_daily batch_start — 历史回填 _explicit_start 标志
**v349**: backfill_range — 按日期范围精准补缺, 与 update_daily 解耦
**v350**: _explicit_start 标志 + conn 共享 + chunk 缺失修复
**v351**: backfill_range 改用 baostock (免费历史数据, qfq 自带)
**v352**: 因子冒烟 95/100 通过
**v354**: ADR-043 因子缓存物化性能 — aux 数据从每日期 12 次 SQL → 每块 12 次 (200x), financial 表加 symbol 过滤, 预计全量物化 3天→3-5小时
**v353**: abn_turnover numpy 数组→pd.Series — 100/100 通过

### 新增因子 (v322-v328)
| 因子 | 类型 | 状态 |
|------|------|------|
| market_beta_60d | low-beta | evaluating |
| overnight_gap_5d | T+1 隔夜动量 | evaluating |
| vol_price_sync_20d | 量价同步 | evaluating |
| revenue_growth_yoy | 营收增长 | evaluating |
| earnings_growth_yoy | 净利润增长 | evaluating |
| piotroski_fscore | 9 项质量打分 | evaluating |
| intraday_reversal | 日内反转 | evaluating (等快照) |
| open_volume_ratio | 开盘量占比 | evaluating (等快照) |
| close_surge | 尾盘异动 | evaluating (等快照) |
| alpha002_vol_div | Alpha#2 | evaluating |
| alpha012_vol_dir | Alpha#12 | evaluating |
| alpha033_gap | Alpha#33 | evaluating |
| alpha035_range_mom | Alpha#35 | evaluating |
| alpha041_geo_vwap | Alpha#41 | evaluating |
| alpha042_vwap_div | Alpha#42 | evaluating |
| alpha055_pos_vol | Alpha#55 | evaluating |

### 数据回填 (v348-v351)
- backfill_range: 2019-01→2022-12, baostock qfq, 3.87M 新行
- 2019: 854→3551 stocks, 2020: 3984, 2021: 4507, 2022: 4935
- adj_factor: 4924/5208 (94.5%), 2020-01-02 起
- tushare 历史数据因 adj_factor 缺失被丢弃 → 归档 ADR-042

### 关键 ADR
- ADR-042: 拒绝纳入的架构/算法 (24 项 + 因子阈值锁定 + 数据回填根因)
- ADR-041: 因子状态机简化
- 见 docs/adr/ 完整列表

### 待完成
- [ ] 全量回测 (2019-2026)
- [ ] 快照数据积累 (60 天后激活日内反转因子)
- [ ] paper trading 桥接验证
