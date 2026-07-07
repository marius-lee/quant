# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-08 01:30 CST

> 旧版归档: docs/HANDOFF-2026-07-02.md / docs/HANDOFF-2026-07-03.md (已 superseded)
> 项目根只有一个 HANDOFF.md 作为单一真相源

---

## 最近提交 (2026-07-08)

| 提交 | 内容 |
|------|------|
| 47fa2df | fix: ThreadPoolExecutor→ProcessPoolExecutor + preloaded_financials dict bug |
| 4dab489 | fix: 因子计算回归顺序循环，彻底消除ThreadPoolExecutor死锁风险 |
| 313fd1a | feat: _init_worker 输出线程启动和数据拷贝确认日志 |
| 32b692b | perf: 恢复ThreadPoolExecutor并行 + per-worker data.copy() 避免MultiIndex行锁 |
| 4bef2ea | fix: 因子计算从 ThreadPoolExecutor 改为顺序循环 |
| 51469e2 | fix: 并行心跳从20改为5个 + 启动确认日志 |
| f0b4b79 | feat: 并行循环加心跳日志 — 每20天/每10因子输出进度 |
| 60e89b4 | perf: IC 计算 + 相关性矩阵并行化 |
| 0467da6 | perf: 因子计算并行化 — ThreadPoolExecutor |
| abaf73a | fix: 修复 Phase 2/3 回测的三个 bug |
| b236d4a | feat: 五阶段标准回测评估完整实现 (CPCV + PBO + Phase 5) |

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
| `factor/stats_cache.py` | ~560 | IC/IR/decay/corr: 因子计算用ProcessPoolExecutor(6进程), IC+相关性用ThreadPoolExecutor |
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
| `stats_cache.py` 因子计算 | for d in dates → ProcessPoolExecutor(6) | 独立进程,无死锁 |
| `stats_cache.py` IC 评估 | for name in factors → ThreadPoolExecutor(6) | ~30s → ~6s |
| `stats_cache.py` 相关性矩阵 | for i,j in pairs → ThreadPoolExecutor(6) | ~10s → ~2s |
| `config.yaml` | `factor.evaluation.max_workers: 6` | 可调整并行度 |

### 并发架构决策 (2026-07-08)

**ThreadPoolExecutor 死锁诊断**:
- 根因: `DataStore._conn` 是单一共享 sqlite3 连接, `check_same_thread=False` 只绕过检查但
  6线程并发 `execute()` 时 sqlite3 内部互斥锁死锁
- 叠加 `pandas.MultiIndex._item_cache` 多线程竞争
- 初期尝试 `data.copy()` per-worker → 无效, 因为 `store._conn` 仍共享
- ThreadPoolExecutor+initializer 12 小时内 4 次提交、反复死锁后最终确定为不可行方案

**最终方案: ProcessPoolExecutor**:
- 因子计算改用 `concurrent.futures.ProcessPoolExecutor`
- 每个 worker 是独立 OS 进程: 自带 Python 解释器、pandas、numpy、sqlite3
- `_pp_worker_init()` 通过 initializer 每个进程只序列化一次 35MB data
- 6 进程 ≈ +300MB 内存, M1 Max 32GB 无压力
- IC 计算和相关性矩阵仍用 ThreadPoolExecutor (小数据结构, 无死锁风险)

终端现在输出心跳日志: 数据加载 → 因子计算(每20天) → IC计算(每10因子) → 相关性矩阵 → 评估结果。

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

- **factor_registry**: 55 因子注册, 31 active / 24 deprecated
- **因子覆盖**: OIR/STR/ABN_TURN/OCFP + P71涨跌停四因子 + P72数据源三因子
- **执行价格**: Sina 实时 open + 除权检测 10% (ADR 017)
- **launchd**: scheduler ✅ / webapp ❌ (须走 restart.sh, ADR 025)
- **数据字典**: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
- **ADR 档案**: [docs/adr/](docs/adr/) (026 条)

---

## 研究资料 (Claude Code 搜索)

| 文件 | 内容 |
|------|------|
| [docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md](docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md) | 154KB, 6合1: 普查+涨跌停+数据源+回测标准+未覆盖因子+微型异象 |
| [docs/research/因子正交化最佳实践_2026-07-08.md](docs/research/因子正交化最佳实践_2026-07-08.md) | 5种方法对比, 推荐对称正交(Löwdin), 天风实证 IR 1.71→2.58 |
| [docs/research/微型异象因子_2026-07-07.md](docs/research/微型异象因子_2026-07-07.md) | 8个方向, 3可落地 (质押delta/可转债隐波/问询函), 5不可 |
| [docs/research/量化因子回测策略业界标准_2026-07-07.md](docs/research/量化因子回测策略业界标准_2026-07-07.md) | CPCV+PBO+walk-forward 标准流程, 已落地为 evaluation/ 包 |

---

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
