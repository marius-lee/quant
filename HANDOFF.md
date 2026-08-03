# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `grep -rn "关键词" HANDOFF.md docs/adr/` 联动搜索，避免重复踩坑。

## 当前状态 (test-v366, 2026-08-03)

### 关键指标
- 因子: 84 注册 (0 active, 20 evaluating, 25 probation, 46 archived — 8 个新注册待评估)
- 数据: 2019-2026 日线, 5208 只 (2019: 3551 只)
- adj_factor: 4924/5208 覆盖 (94.5%), 2020-01-02 起
- 因子缓存: 物化性能优化 A/B/C2 落地; C1(parquet 列存) 保留设计, 待评估后实施
- Alpha 模型: 新增 XGBoost 后端 (combine_mode='xgb'), 与 LightGBM 平行
- 回测: 冒烟通过 (CAGR=-23.5%, 0 errors, avg 2.2 信号/天), 全量待跑
- scheduler: orchestrator (16 tasks, ~85MB) + cron (清空)
- 测试: validate_factors.py 100/100 通过; pytest factor 相关 47/47 通过; pytest alpha/model 19/19 通过

### 晚间链流程
```
19:00 daily_data → adj_factor → factor_cache → attribution → lgb_train(Mon/Thu)
```

### test-v310→v366 变更总览

**v366**: P0 因子正确性修复 — shortcut 公式与原始函数对齐

| 编号 | 项目 | 文件 | 状态 |
|------|------|------|------|
| P0#1 | Piotroski F-Score: 6/9组件魔术索引错误 (total_liability当total_assets) → namedtuple | `missing.py` | ✅ |
| P0#2 | turnover_anomaly shortcut: (MA5-MA60)/std60 替代 (ratio-1) | `_primitives.py` | ✅ |
| P0#3 | idio_vol shortcut: 向量化OLS β回归替代硬编码β=1 | `_primitives.py` | ✅ |
| P0#4 | compute_cf_roa: iloc[0]→iloc[-1] 取最新季度 | `high_priority.py` | ✅ |
| P0#5 | revenue/earnings_growth_yoy: 真YoY替代QoQ | `missing.py` | ✅ |
| P0#6 | _build_fundamentals_panel: ROE推导改用pe_ttm | `store.py` | ✅ |
| P1#7 | reversal shortcut: cum_log替代mean_log | `_primitives.py` | ✅ |
| P1#9 | 删除str shortcut (市值中性化省略, 与原始不等价) | `_primitives.py` | ✅ |

**v365**: (skip — VERSION bump only)

**v364**: 3 个存量问题修复

| 问题 | 修复 | 文件 |
|------|------|------|
| multiprocessing freeze_support (Python 3.14) | ProcessPoolExecutor 显式 `mp_context='fork'` | `store.py` |
| seasonality_12m_1m/tail_risk 多余 `aux` 参数 | 移除未使用的 `aux=None` | `high_priority.py` |
| worker OOM (factor_cache 多进程崩溃) | 因子>50 时 `max_workers` 4→2 + OOM 保护 | `store.py` |

**v363**: 全架构审计 8 项修复 (P0-P3)

| 编号 | 项目 | 文件 | 状态 |
|------|------|------|------|
| P0 | 因子注册签名校验 | `_registry.py` | ✅ |
| P1a | task_runs 装饰器 | `task_log.py` + `snapshot.py` | ✅ |
| P1b | 物化 source_hash 变更检测 | `store.py` | ✅ |
| P2a | 数据质量门禁 | `data/quality.py` + `evening.py` | ✅ |
| P2b | 成本模型接入模拟成交 | `execution_model.py` | ✅ |
| P3a | 因子协方差/冗余检测 | `alpha/model.py` | ✅ |
| P3b | 物化断点续传 | `store.py` | ✅ |
| P3c | 因子 golden 测试集 | `factor/golden_test.py` | ✅ |

**v362**: 4 个基本面因子物化静默失败修复
- 根因: `revenue_growth_yoy`, `earnings_growth_yoy`, `piotroski_fscore`, `cf_roa`
  函数体 `data["close"].columns` 只兼容 MultiIndex, 但 _FUNDAMENTAL_FN_MAP 调度
  传入简单 DataFrame (symbol index, 无 "close" 列) → KeyError → 静默吞错
- 修复 (保留 _FUNDAMENTAL_FN_MAP 归属):
  - 4 函数 `symbols` 提取改为 `isinstance(data.columns, pd.MultiIndex)` 双分支:
    MultiIndex → `data["close"].columns.tolist()`, 简单 DF → `data.index.tolist()`
  - 与同文件 `compute_market_beta_60d`/`compute_overnight_gap_5d` 一致模式
  - 保留 `window=None` 参数 (price dispatch 兼容, 不使用时无副作用)
- 附带: `_str` shortcut 文档标注市值中性化省略

**v361**: orchestrator monitor 崩溃后不重启修复
- `quant/scheduler/orchestrator.py`:
  - `monitor_done` 从 `("ok", "failed")` 改为仅 `"ok"` — monitor 是持续 daemon，"failed" 应重启而非放弃
  - 新增 `_get_monitor_failures()` 统计当日累计 failed+aborted 次数, 达 `_MAX_TASK_RETRIES` 上限后放弃
  - 根因: v313 引入局部 import 导致首次崩溃 → task_runs 写 "failed" → 原逻辑视 "failed" 为完成 → 全天不再重启

**v360**: Bug 修复 — monitor TradeRepo 作用域冲突 + snapshot 死循环
- `quant/scheduler/monitor.py` L253: 删除冗余局部 `from quant.data.repos.trade_repo import TradeRepo`
  - Python 编译时发现函数内局部 import 赋值 → 全函数 TradeRepo 视为局部变量
  - L104 `TradeRepo().get_flag("circuit_breaker")` 时尚未赋值 → UnboundLocalError
  - 顶层已有 `from quant.data.repos import TradeRepo`, 局部导入冗余
- `quant/scheduler/snapshot.py`: `snapshot_open`/`snapshot_close` 添加 `_tk_start/_tk_finish` 写入 task_runs
  - 修复: 未写 DB 导致 orchestrator 每 30s 无限重复触发 (日志确认重复 50+ 次)
  - 新增 `_snapshot_with_log` 包装函数, 含模板 9 日志埋点 (entry/exit/exception + elapsed)

**v359**: XGBoost Alpha 模型后端
- 新增 `quant/alpha/xgb_model.py`: `XgbAlphaModel` (train/predict/save/load/feature_importance)
- `AlphaModel.combine()` 支持 `combine_mode='xgb'`, 未训练/未安装时自动回退 `ic_weighted`
- `quant/config/config.yaml` 新增 `alpha.xgb` 参数块 (reg_lambda=1.0, max_depth=5, subsample=0.8 等量化专用超参)
- 复用 `build_forward_returns` 与因子缓存加载逻辑, API 与 `LgbAlphaModel` 一致
- 测试: pytest alpha/model 19 通过; 模块导入/配置解析正常
**v358**: ADR-043 layer2 — 8因子shortcut化, 消除non-shortcut对MultiIndex data的依赖
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
