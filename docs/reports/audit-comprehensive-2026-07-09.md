# Quant 项目全景审计报告

**日期**: 2026-07-09 | **版本**: VERSION 31 | **审计范围**: 85 个 Python 源文件, ~15,300 行

---

## 1. 项目定位

A股量化选股系统。基于 Grinold & Kahn 主动投资组合管理理论，7 层架构每天自动化运行：数据同步 → 因子计算 → Alpha 合成 → 风控过滤 → 组合优化 → 模拟执行 → 监控报告。

**北极星**: ¥5,000 → ¥1,000,000（200 倍），单人在 M1 MacBook 8GB 上运行。

---

## 2. 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.14 |
| 数据 | NumPy, Pandas, SciPy, SQLite（market.db ~1.3GB + trades.db ~1MB） |
| ML | scikit-learn, LightGBM, XGBoost |
| Web | Flask（端口 8521） + Plotly.js + SSE 推送 |
| 缓存/消息 | Redis（跨进程 pub/sub + 流控） |
| 数据源 | Tickflow → 新浪 → 腾讯 → Tushare → akshare（5 级回退链） |
| 配置 | YAML 热加载 + ${ENV} 变量替换 |
| 调度 | launchd（macOS） + 内置三阶段 daemon 线程 |
| 测试 | pytest（仅 3 个测试文件） |

---

## 3. 核心文件（按重要性排序）

### 修改前必须读懂

| 优先级 | 文件 | 说明 |
|--------|------|------|
| ★★★★★ | `pipeline.py` (560行) | 总调度器，串联 7 层，每个 step 独立 try/except |
| ★★★★★ | `data/store.py` (1500行) | 数据中心，多源增量同步 ~5500 只股票 |
| ★★★★★ | `factor/compute.py` (3131行) | 55 个因子计算函数 + `compute_all_factors()` 入口 |
| ★★★★ | `execution/engine.py` (200行) | 模拟执行引擎 + 成本计算 + T+1/除权检测 |
| ★★★★ | `data/trade_repo.py` (239行) | trades.db 统一读写，资金管理，持仓查询 |

### 次重要

| 文件 | 说明 |
|------|------|
| `factor/stats_cache.py` (730行) | IC/IR 评估缓存，含 ProcessPoolExecutor |
| `optimizer/portfolio.py` (320行) | 资金自适应组合优化器 |
| `scheduler.py` (220行) | 三阶段交易日调度器 |
| `web/app.py` (650行) | Flask API + dashboard |

---

## 4. 功能清单

### ✅ 已完整实现

- 7 层架构全串联（每层独立容错）
- 55 个因子注册（39 价格 + 16 基本面），31 active
- 4 种 Alpha 合成方式（IC加权/等权/交集/Sleeve）
- 行业/市值中性化 + Ledoit-Wolf 协方差 + 风控约束
- 资金自适应优化器（<2万等权 → 2-10万得分倾斜 → >10万均值-方差）
- 统一成本模型（佣金万三 + 印花税千一 + 滑点千一）
- Brinson 归因 + Sharpe/Drawdown/WinRate
- Web 仪表盘（4 Tab，SSE 实时推送，Plotly 图表）
- 三阶段调度器（08:30 信号 → 09:30 执行 → 15:30 归因）
- 多日回测（`backtest.py`）
- 5 阶段因子评估标准流程（CPCV + PBO + Walk-Forward）
- 22 份 ADR 文档 + 16 份研报文档
- 数据字典（`docs/DATA_DICTIONARY.md`）

### ⚠️ 待实现

1. **`eval_standard.sh` 对全量 active 因子执行 Phase 2+3** — 评估当前 35 active 因子
2. **对称正交化（Lowdin）** — 若通过 Phase 2+3 的因子太少
3. **微型异象因子** — 质押比率变化、可转债隐含波动率、问询函
4. **Phase 5 monitor 接入每日调度** — 目前仅手动 `--phase5` 运行
5. **`alpha/model.py` 空壳** — AlphaModel 类不存在，逻辑内联在 `pipeline.py` 中
6. **launchd webapp 自动重启** — ADR 025 标记为不实施

---

## 5. 架构评估

### 当前架构：良好，不需要大规模重构

7 层架构清晰、层间解耦、配置驱动。

### 建议改进

| 优先级 | 改进 | 理由 |
|--------|------|------|
| **高** | `alpha/` 包清理 | 当前是空壳，逻辑在 `pipeline.py` 内联，应独立为 `AlphaModel` 类 |
| **高** | 因子注册表集中化 | 因子分散在 `_PRICE_FN_MAP`、`_FUNDAMENTAL_FN_MAP` 和文件末尾动态注册 3 处 |
| **中** | `data/` 连接层统一 | 多处绕过 DataStore 直接开 sqlite3.connect，应统一走 DataStore |
| **中** | `pipeline.py` 拆分 | 560 行做太多事，Step 3 Alpha 逻辑应移到 `alpha/model.py` |
| **低** | 消除重复定义 | 5 个函数在 `compute.py` 定义了 2 次 ~200 行死代码 |

### 不需要改的

- 7 层架构模式 —— 业界标准，清晰合理
- YAML 配置驱动 —— 零硬编码（已审计通过）
- 成本模型独立 —— 正确抽离
- Web dashboard SPA —— 功能完整

---

## 6. 代码质量问题

### 🔴 Critical（3 个）

1. **SQL 注入风险（9 处）** — `compute_sue()`、`compute_ztd()`、`compute_northbound_flow()`、`compute_asset_growth()`、`compute_holder_reduction()`、`compute_pledge_ratio()`、`compute_dividend_yield()`、`compute_str()`（2处）使用 f-string 拼接 SQL，未参数化。文件: `factor/compute.py`
2. **stderr 全部吞入 /dev/null** — `stats_cache.py:96` 的 `_pp_compute_chunk` 中 `sys.stderr = open(os.devnull, 'w')` 不恢复，所有 worker 错误静默丢失
3. **`optimizer/__init__.py` 导出断裂** — `TargetPortfolio` 和 `LOT_SIZE` 在 `__all__` 中声明但未 import

### 🟡 Medium（7 个代表性问题）

4. **`compute.py` 5 个函数重复定义** — `_get_financial_historical`、`_ttm_sum`、`compute_gross_margin_diff`、`compute_financial_anomaly`、`compute_roe_trimmed` 各定义 2 次，第一次定义 ~200 行死代码（行 1448-1653）
5. **`TradeRepo.record_trade` 重复定义** — 第 130 行（dict 版，strategy="chen"）和第 189 行（参数版），前者是死代码
6. **`DataStore._connect()` 无线程锁** — `check_same_thread=False` 允许多线程但无 mutex 保护
7. **`ExecutionEngine.execute()` 无事务回滚** — 批量订单部分失败时数据库状态不一致
8. **`engine.py` SQLite 直连绕过 DataStore** — `_check_ex_dividend` 用原始 `sqlite3.connect`，缺少 WAL mode
9. **多处硬编码路径** — `evaluation/`、`scripts/` 中 5 处硬编码 `"data/market.db"`
10. **`data/store.py` 硬编码延迟** — `time.sleep(0.8)`、`time.sleep(1.5)`、`max_days=2000`、IP `180.153.18.170` 应改为配置驱动

### 🟢 Low（5 个代表性问题）

11. **测试覆盖极薄** — 仅 3 个 test 文件（`test_factor_compute`、`test_marginal`、`test_synth`），无集成测试，无 DataStore/Engine/Quote/Risk/Optimizer 测试
12. **50+ 处 `except Exception:`** — `data/store.py`(16), `web/state_broker.py`(10), `web/app.py`(7), `factor/compute.py`(7)
13. **`config/` 缺少 `__init__.py`** — 与项目其他包不一致
14. **文件锁无清理机制** — `stats_cache.py` 的 `.factor_compute.lock` 在进程被杀后残留
15. **`_shared_limit_conn` 导入但从未使用** — `factor/compute.py:30`

### 内存泄漏（已修复）

- **ProcessPoolExecutor 指数级级联** — `stats_cache.py` `compute_factor_stats()` 被并发调用 6 次，每次 spawn 6×N 进程，导致 81 进程 / 2.2GB RSS。修复：文件锁 + `_MAX_WORKERS` 上限 4 + `executor.shutdown(wait=True)` + warmup 不强制重算 + scheduler 跳过已过阶段

---

## 总体评级

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | ⭐⭐⭐⭐⭐ | 7 层清晰，配置驱动，学术界-工业界对齐 |
| 功能完整性 | ⭐⭐⭐⭐ | 端到端可用，Alpha 层有空壳，Phase 5 未自动化 |
| 代码质量 | ⭐⭐⭐ | 向量化计算优秀，但有 SQL 注入、重复代码、测试缺失 |
| 文档完整性 | ⭐⭐⭐⭐⭐ | 22 ADR + 16 研报 + 数据字典 + 审计报告 |
| 可运维性 | ⭐⭐⭐ | launchd + daemon 调度可用，但无告警、无集成测试 |
