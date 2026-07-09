# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-09 15:00 CST

> 旧版归档: docs/HANDOFF-2026-07-02.md / docs/HANDOFF-2026-07-03.md (已 superseded)
> 项目根只有一个 HANDOFF.md 作为单一真相源

---

## 最近提交 (2026-07-08)

| — | fix: ProcessPoolExecutor 孤儿进程泄漏 — 3层防护 + 双调度器清理 (P68) |
| — | fix: web/app.py SIGTERM handler 增加子进程清理 |
| — | fix: stats_cache.py 新增 PID 追踪 + _cleanup_process_pool() |
| — | chore: 删除 scheduler.py / restart.sh / launchd plists |

| 提交 | 内容 |
|------|------|
| 736decd | perf: 主进程不加载全量 OHLCV — worker 返回 close, 内存 ~5GB→~200MB, 消除 swap |
| 0b62c3a | fix: 移除 ProcessPoolExecutor 600s 超时 — 500股×120天被误杀, 恢复五阶段全流程 |
| 5ec691d | fix: stats_cache 模块级 sys.path 守卫 — spawn worker 确保能找到项目模块 |
| 8e64647 | fix: vol_price_corr 除零保护 — std()>0 检查 |
| 498c88b | fix: epa 因子注册 — _FUNDAMENTAL_FN_MAP→_PRICE_FN_MAP |
| a6f366a | test: 500 stocks × 120 days — 中等规模验证 |
| bb3dfce | fix: pair_corrs 闭包变量泄漏 + 重复 store.close() |
| ed203e1 | fix: ic_series + corr_counts 未初始化 NameError |
| 1a20eee | fix: ProcessPoolExecutor 正确实现 — worker 自加载 (ADR 027) |
| c20bfee | debug: 移除 worker logger — 隔离 logging 锁嫌疑 |
| bd78c68 .. 72bc69e | ThreadPoolExecutor 死锁调试 (7 次提交, 已废弃) |
| b236d4a | feat: 五阶段标准回测评估 (CPCV + PBO + Phase 5) |

---

## 核心架构

```
layer 0: data/       — akshare 数据拉取 + store
layer 1: factor/     — 因子计算 + 合成 + 评估缓存
layer 2: risk/       — 行业/市值中性化
layer 3: alpha/      — pipeline 调度 (当前隐含在 pipeline.py 中)
layer 4: execution/  — 下单引擎 + 除权检测
layer 5: web/        — Flask 前端
layer 8: evaluation/ — 五阶段回测评估 (新增)
```

### factor/ 模块 (compute.py 拆分后)

| 文件 | 行数 | 职责 |
|------|------|------|
| `factor/compute.py` | ~2550 | 全部因子函数 + maps + compute_all_factors |
| `factor/registry.py` | 45 | _cs_zscore, _db_connect, _FIN_FACTORS |
| `factor/orchestrator.py` | 25 | get_factor_names (延迟导入) |
| `factor/synth.py` | — | equal_weight, ic_weighted, sleeve_compose |
| `factor/stats_cache.py` | ~530 | IC/IR/decay/corr: 因子计算 ProcessPoolExecutor(6进程自加载DB), IC+相关性 ThreadPoolExecutor |
| `factor/__init__.py` | 55 | __getattr__ 惰性导入, 打破循环 |
| `config/constants.py` | 80 | 全局常量 + _require_cfg + _market_db_path |

### evaluation/ 包 (五阶段回测)

| 文件 | 职责 |
|------|------|
| `evaluation/phase1_data.py` | 股票池验证 + 数据范围 |
| `evaluation/phase2_single.py` | IC/\|t\|/ICIR/half-life 四维筛选 |
| `evaluation/phase3_oos.py` | CPCV walk-forward + PBO 检验 |
| `evaluation/phase4_costs.py` | 交易成本后验证 |
| `evaluation/phase5_monitor.py` | 持续监控 (拥挤度/衰减/换手率/容量) |
| `evaluation/cpcv.py` | Purged WF-CV (De Prado 2018 Ch.7) |
| `evaluation/pbo.py` | PBO + DSR (De Prado 2018 Ch.8) |

---

## 性能改进

| 模块 | 改动 | 效果 |
|------|------|------|
| `stats_cache.py` 因子计算 | ThreadPoolExecutor → ProcessPoolExecutor (worker 自加载, ADR 027) | ~140s → ~16s (9×加速) |
| `stats_cache.py` IC 评估 | for name in factors → ThreadPoolExecutor(6) | ~30s → ~6s |
| `stats_cache.py` 相关性矩阵 | for i,j in pairs → ThreadPoolExecutor(6) | ~10s → ~2s |
| `config.yaml` | `factor.evaluation.max_workers: 6` | 可调整并行度 |

### 并发架构 (最终方案 ADR 027, 2026-07-08)

**ProcessPoolExecutor worker 自加载** — ADR 027:

- 主线程只传元数据 (symbols + date_strs + factor_names, <10KB) → ZERO DataFrame pickling
- 每个进程打开独立 DataStore (WAL 并发读), 加载 daily + fundamentals + financials
- 6 进程 × 独立 GIL → 真正 OS 级并行 (~9× 加速 vs ThreadPoolExecutor)
- 主进程不再加载 OHLCV: eval_dates 从 SQL DISTINCT date 获取, close 由 worker 返回后拼接
  (全量 5493 股时主进程内存从 ~5GB 降至 ~200MB, swap 消除)
- 主线程 `store.close()` 在 spawn 前调用, 无 WAL 锁继承
- **as_completed 不加 timeout**: ProcessPoolExecutor 段去掉了 600s 超时 (af2d24e 引入)。
  500 股×120 天单 chunk 需 ~700s, 600s 超时误杀正常计算。worker 内层 try/except 已兜底,
  系统级崩溃极罕见且超时无法恢复。IC/相关性 ThreadPoolExecutor 段保留 timeout。

**已废弃**:
- ThreadPoolExecutor stateless worker → GIL 串行化, 6 线程 = 1 线程性能
- ProcessPoolExecutor initargs 传 DataFrame (187MB/35MB) → pickle 启动延迟 1-2min
- ThreadPoolExecutor 共享 DataStore._conn → 死锁

**保留 ThreadPoolExecutor 用于**: IC 计算和相关性矩阵 (轻量级, ≤6s 完成)

---

## 回测标准 (ADR 026)

**流程**: `PYTHONPATH=. bash scripts/eval_standard.sh [--phase5]`

| 阶段 | 方法 | 阈值 |
|------|------|------|
| Phase 1 | 全A 5493只, ST由pipeline过滤, 含退市股 | backtest_start_date=2010-01-01 |
| Phase 2 | 单因子 IC/\|t\|/ICIR/half-life | \|IC\|≥0.02, \|t\|≥2.0, ICIR≥0.5, HL≥20d |
| Phase 3 | CPCV N=5, embargo=1d + PBO | logit(PBO) < -0.847, Sharpe decay <50% |
| Phase 4 | 扣费后 Sharpe 确认 | Net Sharpe > 0.3 |
| Phase 5 | 监控报告 (可选 --phase5) | 拥挤度/IC衰减/换手率/容量 |

---

## 当前状态

- **config.yaml**: n_symbols=0 (全量 A 股回测, ADR 026 标准配置)
- **并发**: ProcessPoolExecutor worker 自加载 (ADR 027), 6 进程, 无 as_completed timeout (500×120 单 chunk ~700s, 误杀风险 > 兜底价值)
- **factor_registry**: 64 因子注册, 35 active / 23 deprecated
- **因子覆盖**: OIR/STR/ABN_TURN/OCFP + 涨跌停六因子 + EPA/TRCF/ideal_amplitude + margin_buy_ratio (融资买入占余额比, margin_detail 表, 广发2024) + analyst_consensus (分析师共识度, analyst_forecast 表, 中信建投2022) + EPD/EPDS (估值偏离) + 毛利率TTM差分/财务异常复合/单季度ROE(掐头) (Phase 3 财务三因子)
- **已修复 4 bug**: epa 注册错误 / ocfp 签名不匹配 / vol_price_corr 除零 / seal_time 格式越界
- **执行价格**: Sina 实时 open + 除权检测 10% (ADR 017)
- **launchd**: scheduler ✅ (KeepAlive) / webapp ❌ (须走 restart.sh, ADR 025)
- **SIGTERM 安全**: _clean_exit() 先清 ProcessPoolExecutor 子进程再退出, .compute_pids 追踪 worker PID (P68)
- **数据字典**: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
- **ADR 档案**: [docs/adr/](docs/adr/) (027 条)
- **备份**: factor/stats_cache.py.bak (ThreadPoolExecutor 版本, 已废弃)

---

## 研究资料 (Claude Code 搜索)

| 文件 | 内容 |
|------|------|
| [docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md](docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md) | 154KB, 6合1: 普查+涨跌停+数据源+回测标准+未覆盖因子+微型异象 |
| [docs/research/因子正交化最佳实践_2026-07-08.md](docs/research/因子正交化最佳实践_2026-07-08.md) | 5种方法对比, 推荐对称正交(Löwdin), 天风实证 IR 1.71→2.58 |
| [docs/research/微型异象因子_2026-07-07.md](docs/research/微型异象因子_2026-07-07.md) | 8个方向, 3可落地 (质押delta/可转债隐波/问询函), 5不可 |
| [docs/research/量化因子回测策略业界标准_2026-07-07.md](docs/research/量化因子回测策略业界标准_2026-07-07.md) | CPCV+PBO+walk-forward 标准流程, 已落地为 evaluation/ 包 |

---

## 最近评估结果 (2026-07-08)

- **Phase 2**: 31 因子全量评估, zt_streak 唯一通过 (IC=+0.0556, t=8.2, IR=+0.75, HL≈26d), 耗时 2485s
- **Phase 3**: CPCV N=5, PBO=0.000, OOS_ICIR=+0.808, 通过
- **Phase 4**: 扣费后验证通过
- **Phase 5**: 监控报告已生成 docs/reports/monitor_2026-07-08.md

## 最近修复 (2026-07-09)

### P68: ProcessPoolExecutor 孤儿进程内存泄漏 — 根因修复

详情见 CHANGELOG.md §3.6.0.

**3 层防护**: (1) executor.shutdown(wait=True); (2) PID 文件 .compute_pids 追踪, 模块加载/崩溃时自动清理; (3) web/app.py SIGTERM handler → _clean_exit() 读文件杀所有 worker PID.

**清理**: 删除 scheduler.py (根目录 standalone) / restart.sh / 三个 launchd plist, 双调度器问题已消除.

## 下一步计划

1. **立即可做**: 运行 `eval_standard.sh` 拿 Phase 2+3 结果, 看几个因子通过
2. **取决于1**: 如果通过的因子少 → 做对称正交化(Löwdin), 捞被埋没的因子
3. **如果1+2 后效果仍不够** → 接入质押比例变化(delta), 可转债隐波差
4. **调度**: Phase 5 monitor 配入 scheduler 每日盘后运行

## 关键约束

- 修改前先备份 + 提交 git
- 数值参数放 config/config.yaml, 永不硬编码
- 永不 fallback 执行价格
- 因子 status 变更记入 notes 字段 (追加式)
- 修改后文档同步更新, 根 HANDOFF.md 是唯一真相源
