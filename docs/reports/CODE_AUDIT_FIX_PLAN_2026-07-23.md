<!-- 修复方案 — 基于 CODE_AUDIT_REPORT_2026-07-23.md -->
# Quant 项目审计修复方案
**审计版本**: test-v236 | **方案日期**: 2026-07-23 | **状态**: 待讨论

---

## 修复优先级总览

按报告建议的 P0→P1→P2→P3 顺序排列，共 33 Bug + 9 架构问题 + 7 逻辑错误 = 49 项。

| 优先级 | 数量 | 类别 |
|--------|------|------|
| 🔴 P0 立即 | 10 | B1-B8(崩溃级) + L2(收益率计算错误) + L3(负alpha bug) |
| 🟡 P1 本周 | 15 | B9-B23(中等) + A1/A2/A3/A7 + L4/L5/L6/L7 |
| 🟢 P2 本月 | 10 | B24-B33(轻微) + A4/A5/A6/A8 |
| ⚪ P3 下季度 | 14 | 架构A9 + 算法优化 + 新技术引入 |

---

## 🔴 P0：立即修复（共 10 项）

---

### B1 — pipeline.py:191-192 — 空候选集崩溃

**问题**: 当 `symbols` 为空时 `data.iloc[:0]` 丢失 MultiIndex 结构，后续列计算崩溃。

**根因**: `iloc[:0]` 在 MultiIndex DataFrame 上不保留列层级信息。

**修复方案**:
- 空 symbols 时 `return {}` 提前退出，不进入后续切片计算。
- 在 `generate_signals()` 开头加 `if not symbols: return {"targets": []}`。

**涉及文件**: `quant/pipeline.py`，~L191-192

**工时**: 15 分钟 | **风险**: 低

---

### B2 — backtest/loop.py:50-52 — CAGR 计算使用 252 交易日

**问题**: `years = len(returns) / 252` 使用美股 252 日/年，A 股实际年均 ~244 个交易日。CAGR 被高估约 3.3%。

**修复方案**:
- 将 252 替换为从配置读取的 `TRADING_DAYS_PER_YEAR`。
- `config.yaml` 新增 `backtest.trading_days_per_year: 244`。
- 或在 calendar.py 中新增 `trading_days_in_range(start, end)` 精确计算。

**涉及文件**: `backtest/loop.py:50-52`, `quant/config/config.yaml`

**工时**: 20 分钟 | **风险**: 低（回测 CAGR 数值会变）

---

### B3/L3 — alpha/model.py:134-138 — Quadratic Decay 负 alpha bug

**问题**: `alpha[below] = alpha[below] * (alpha[below] / threshold) ** 2` — 负 alpha 平方后变正，本应被衰减的负信号反而被增强。

**修复方案 (二选一)**:
- **方案 A (最小改动)**: 加 `np.maximum(alpha[below], 0)` 保护，负 alpha 直接置零。
- **方案 B (报告推荐)**: Sigmoid Soft Cutoff: `α' = α / (1 + exp(-k*(α - T)))`，平滑过渡。

**建议**: 先用方案 A 快速修复 bug，方案 B 作为 P2 算法优化。

**涉及文件**: `alpha/model.py:134-138`

**工时**: 方案A 15分钟，方案B 1小时 | **风险**: 中（影响 alpha 排名）

---

### B4 — quant/data/jq_valuation.py:140,147 — `_cache.put()` 应为 `.set()`

**问题**: `DataCache` 方法名是 `.set()` 不是 `.put()`，运行时抛 `AttributeError`。估值缓存完全失效。

**修复方案**:
- `_cache.put(date_str, raw)` → `_cache.set(date_str, raw)`
- 两处（L140, L147）

**涉及文件**: `quant/data/jq_valuation.py`

**工时**: 5 分钟 | **风险**: 无（当前缓存无效，修复后数据源拉取可能更快）

---

### B5 — quant/data/store.py:1532 — `validate_date_format(trade_date, ...)` 变量未定义

**问题**: 前一行的 `trade_date` 被注释掉（"no-op removed"），但下一行仍引用该变量，导致 `NameError`。

**修复方案**:
- 重新定义 `trade_date` 变量，或从上下文获取。
- 该处的 `validate_date_format` 调用目的是验证 `trade_date` 格式，需要传入正确的日期参数。

**涉及文件**: `quant/data/store.py:1529-1534`

**工时**: 15 分钟（需分析上下文确定正确值）| **风险**: 低（当前 LHB 同步直接崩溃）

---

### B6 — quant/data/store.py:1482-1491 — `sync_all(max_pb_fetch=-1)` 参数名错误

**问题**: `sync_all()` 的参数名是 `max_fetch`，不是 `max_pb_fetch`，抛 `TypeError`。基本面同步崩溃。

**修复方案**:
- `max_pb_fetch=-1` → `max_fetch=-1`

**涉及文件**: `quant/data/store.py:1482`

**工时**: 5 分钟 | **风险**: 无

---

### B7 — 两个不兼容的 TradeRepo 类

**问题**: `quant/data/repos/trade_repo.py` 和 `quant/data/trade_repo.py` 两个文件各定义了一个 TradeRepo 类，schema 完全不同。
- `repos/` 版: key-value store (`key TEXT, value TEXT`)
- `data/` 版: 结构化列 (`initial_capital`, `max_positions`)

**根因**: 看起来是半途迁移，`repos/` 版是新的代码结构但实现未完成，`data/` 版是旧的但业务在用。

**修复方案**:
1. 确认当前代码哪些模块 import 了哪个 TradeRepo
2. 将两版 schema 对齐到 `repos/` 版（结构化列），删除 `data/` 中的旧 schema
3. 统一所有 import 路径为 `from quant.data.repos.trade_repo import TradeRepo`
4. `repos/` 中补充 `data/` 中已有的业务方法（`save_position_meta`, `get_pending_orders` 等）

**涉及文件**: `quant/data/repos/trade_repo.py`, `quant/data/trade_repo.py`, 所有 import TradeRepo 的模块

**工时**: 2 小时 | **风险**: 高（涉及多模块 schema 对齐，需要仔细测试）

---

### B8 — quant/utils/logger.py:73 — Python 3.13+ 依赖

**问题**: `logging.Formatter(defaults={"trace_id": ""})` 使用 Python 3.13+ 新增的 `defaults` 参数，低版本 Python 抛 `TypeError`。

**修复方案**:
- 用自定义 Formatter 子类替代 `defaults` 参数，兼容 Python 3.10+:
  ```python
  class TraceFormatter(logging.Formatter):
      def format(self, record):
          if not hasattr(record, 'trace_id'):
              record.trace_id = ''
          return super().format(record)
  ```
- 或在 `pyproject.toml` 中声明 `python_requires >= 3.13`

**涉及文件**: `quant/utils/logger.py:73`, `pyproject.toml`

**工时**: 15 分钟 | **风险**: 低

---

### L2 — backtest/loop.py:306-308 — 收益率计算错误

**问题**: `next_ret = _get_prices(..., field="close")` 获取次日收盘**价**，但 `record_day()` 将其作为**收益率**使用。

**修复方案**:
- `next_ret = next_close / today_close - 1` 转换为收益率。
- 或者将 `_get_prices` 改为直接返回收益率。

**涉及文件**: `backtest/loop.py:302-308`

**工时**: 10 分钟 | **风险**: 中（回测权益曲线会变）

---

**P0 总计工时**: ~5 小时

---

## 🟡 P1：本周修复（共 15 项）

---

### B9 — pipeline.py:1 — dead import

**方案**: 删除 `import traceback`，该模块未在任何地方使用。

**涉及文件**: `pipeline.py:1` | **工时**: 1 分钟

---

### B10 — pipeline.py:418-419 — 重复 pd.Series 调用

**方案**: 删除第二次 `pd.Series(prices)` 调用，结果被下一行立即覆盖。

**涉及文件**: `pipeline.py:418-419` | **工时**: 1 分钟

---

### B11 — daily_sync.py:30 — step1_ohlcv 忽略 date_str 参数

**问题**: 函数接受 `date_str` 但忽略它，始终用 `start="2020-01-01"` 全量拉取。

**方案**: 将 `date_str` 传入 `DataStore.update_daily()` 的 `start` 参数，或添加增量模式。

**涉及文件**: `quant/daily_sync.py:30` | **工时**: 10 分钟

---

### B12 — daily_sync.py:67 — step5_fundamentals 用 datetime.now() 而非 date_str

**方案**: 改为基于传入 `date_str` 判断，或新增 `force_weekly` 参数。

**涉及文件**: `quant/daily_sync.py:67` | **工时**: 10 分钟

---

### B13/B14 — fund_flow.py / margin.py — 死重试循环

**问题**: 手动 `for attempt in range(max_retries)` 循环是死代码，因为 `@datasource_retry` 已在外层处理重试。每条路径都在第一次迭代 return。

**方案**: 删除手动重试循环，只保留单次调用 + `@datasource_retry`。

**涉及文件**: `fund_flow.py:61-100`, `margin.py:116-155` | **工时**: 15 分钟

---

### B15 — benchmark.py:49-50 — SQLite 连接泄露

**方案**: 使用 context manager (`with self.conn:`) 或 `try/finally` 确保连接关闭。

**涉及文件**: `quant/data/benchmark.py:49-50` | **工时**: 5 分钟

---

### B16 — news.py:89,99 — SQLite 连接泄露

**方案**: 同上，`news_df.empty` 时提前 return 前关闭连接。

**涉及文件**: `quant/data/news.py:89,99` | **工时**: 5 分钟

---

### B17 — excepthook.py:31 — setup() 非 idempotent

**方案**: 加 `if _hook_installed: return` 守卫，防止重复包装。

**涉及文件**: `quant/utils/excepthook.py:31` | **工时**: 5 分钟

---

### B18 — excepthook.py:38-40 — 回测异常静默吞没

**问题**: 回测上下文中的 unhandled exception 被捕获但进程不设非零退出码，CI 无法检测失败。

**方案**: 异常处理末尾加 `os._exit(1)` 或设置全局标志，由调用方检测。

**涉及文件**: `quant/utils/excepthook.py:38-40` | **工时**: 10 分钟

---

### B19 — datasource_retry.py:9 — docstring 声称 jitter 未实现

**方案**: 在 backoff 中加 `random.uniform(0, 1)` 抖动，或更新 docstring。

**涉及文件**: `quant/data/datasource_retry.py:9` | **工时**: 10 分钟

---

### B20 — cache.py:201 — DataCache.invalidate() 空分支

**方案**: 实现 `raw_key=None` 时的全量清除逻辑，遍历所有 key 并删除。

**涉及文件**: `quant/data/cache.py:201` | **工时**: 10 分钟

---

### B21 — store.py:1693-1697 — 52 周高点用 365 天

**方案**: 改为从 config 读取 `TRADING_DAYS_PER_YEAR`（244），或用 trading calendar 精确计算。

**涉及文件**: `quant/data/store.py:1693-1697` | **工时**: 10 分钟

---

### B22 — config/loader.py:128 — null 值报错信息误导

**方案**: 区分 "key 不存在" 和 "key 为 null"，null 报 `ValueError` 而非 `KeyError`。

**涉及文件**: `quant/config/loader.py:128` | **工时**: 10 分钟

---

### B23 — stop_loss.py:106-108 — TP1 半仓卖出可能非整手

**方案**: 将 `sell_shares = shares // 2` 向下取整到 100 的倍数: `sell_shares = (shares // 2 // 100) * 100`。

**涉及文件**: `quant/execution/stop_loss.py:106-108` | **工时**: 5 分钟

---

### L4 — optimizer/portfolio.py:95 — _iterative_clip 等权无归一化

**方案**: 在返回等权前加 `w = w / w.sum()`。

**涉及文件**: `quant/optimizer/portfolio.py:95` | **工时**: 5 分钟

---

### L5 — risk/neutralize.py:95 — OLS 极端市值数值不稳定

**方案**: 对市值取对数或 winsorize(1%/99%)，或改用 `HuberRegressor`。

**涉及文件**: `quant/risk/neutralize.py:95` | **工时**: 30 分钟

---

### L6 — alpha/model.py:66-67 — abs(ic_60d) > 1e-10 阈值太宽松

**方案**: 增大到 `1e-5` 并加 `clip(0, 1)`。

**涉及文件**: `alpha/model.py:66-67` | **工时**: 5 分钟

---

### L7 — optimizer/rebalance.py:189 — validate_orders 成本计算不对称

**方案**: 分 buy/sell 分别计算成本，`cash -= o.shares * px + cost_model.buy_cost(px, o.shares)` 只对买入扣除。

**涉及文件**: `quant/optimizer/rebalance.py:189` | **工时**: 20 分钟

---

**P1 总计工时**: ~3 小时

---

## 🟢 P2：本月修复（共 10 项）

---

### B24 — pipeline.py:35 — 模块级 web 导入耦合

**方案**: 用延迟导入（函数内 `from web.state_broker import broker`）或提供 stub。

**涉及文件**: `pipeline.py:35` | **工时**: 10 分钟

---

### B25 — pipeline.py:39 — LOT_SIZE 模块级加载

**方案**: 移到函数内懒加载，避免 `import pipeline` 时报错。

**涉及文件**: `pipeline.py:39` | **工时**: 5 分钟

---

### B26 — orchestrator.py — wait_m 未使用

**方案**: 删除未使用变量。

**涉及文件**: `scheduler/orchestrator.py` | **工时**: 5 分钟

---

### B27 — monitor.py:25-28 — 阈值硬编码

**方案**: 改为 `_require_cfg("monitor.max_drawdown_pct")` 等。

**涉及文件**: `scheduler/monitor.py:25-28`, `config.yaml` | **工时**: 15 分钟

---

### B28 — kelly.py:76 — DEFAULT_RETURN_VAR 硬编码

**方案**: 从协方差矩阵动态估算: `np.mean(np.diag(cov_matrix))` 或传入参数。

**涉及文件**: `optimizer/kelly.py:76` | **工时**: 15 分钟

---

### B29 — web/app.py:92-96 — 持仓查询性能

**方案**: 用 `LEFT JOIN sim_trades ... WHERE sim_trades.symbol IS NULL` 替代 `NOT IN` 子查询。

**涉及文件**: `web/app.py:92-96` | **工时**: 15 分钟

---

### B30 — store.py:1402 — LRU _query_cache 无大小上限

**方案**: 添加 `if len(cache) > MAX_CACHE_SIZE: cache.popitem(last=False)`。

**涉及文件**: `store.py:1402` | **工时**: 5 分钟

---

### B31 — cache.py:78 — Token bucket 丢弃分数 token

**方案**: 累计分数 token，不丢弃。

**涉及文件**: `cache.py:78` | **工时**: 10 分钟

---

### B32 — _dispatch.py:101-103 — preloaded_financials 为 None 时报错不友好

**方案**: 加显式的 `if preloaded_financials is None: raise ValueError(...)`。

**涉及文件**: `factor/compute/_dispatch.py:101-103` | **工时**: 5 分钟

---

### B33 — analyst.py:32-34 — eps_20xx 列名硬编码

**方案**: 动态生成 `[f"eps_{y}" for y in range(current_year, current_year+3)]`。

**涉及文件**: `analyst.py:32-34` | **工时**: 10 分钟

---

**P2 总计工时**: ~1.5 小时

---

## 架构问题逐项修复方案

---

### A1 — 无依赖注入 / IoC 容器 🔴

**问题**: 所有依赖通过 `from X import Y` 模块级硬编码，导致测试困难、紧耦合。

**方案**:
- 引入 `StrategyContext` dataclass，封装 `store: DataStore, engine: ExecutionEngine, cost_model: CostModel, trade_repo: TradeRepo` 等核心依赖。
- Pipeline、Scheduler、Backtest 均通过 `ctx` 参数传入依赖，而非模块级 `from X import Y`。
- 测试时注入 mock 对象。

**涉及文件**: 新建 `quant/context.py`，修改 `pipeline.py`, `scheduler/*.py`, `backtest/*.py`

**工时**: 4 小时 | **风险**: 高（大范围重构）

---

### A2 — 全局状态过多 🔴

**问题**: `_CACHE`, `_query_cache`, `_backend`, `_source_speed` 等模块级可变状态。

**方案**:
- 将缓存封装为有生命周期的对象（`CacheManager`），在 app 启动时创建，关闭时销毁。
- 数据源速度追踪封装为 `SourceSpeedTracker` 类。
- 逐步迁移，不一次性重构。

**涉及文件**: `store.py`, `cache.py`, `datasource_retry.py`

**工时**: 3 小时

---

### A3 — generate_signals() 参数膨胀 🟡

**问题**: 16 个参数包括内部优化参数如 `primitives`, `skip_pull` 等。

**方案**:
- 用 `StrategyContext` 封装 data/engine/store/repo 等系统性依赖。
- 其余控制参数用 `SignalOptions` dataclass 封装（`skip_pull`, `symbols`, `force` 等）。
- 两者合并为单个 `ctx` 参数。

**涉及文件**: `pipeline.py`, `context.py`（新建）

**工时**: 2 小时

---

### A4 — Scheduler 与 Pipeline 职责重叠 🟡

**问题**: `orchestrator → signals_run → pipeline.generate_signals` 三跳调用。

**方案**: 合并 `signals_run` 和 `pipeline.generate_signals` 为一个入口，或保持但用装饰器注册模式。

**涉及文件**: `scheduler/signals.py`, `scheduler/orchestrator.py`

**工时**: 1 小时

---

### A5 — 错误处理不一致 🟡

**问题**: Pipeline 中每层有 try/except，但 backtest 中异常直接崩溃。

**方案**: 统一错误处理策略：
- Pipeline 层: try/except → 记录日志 → 返回空结果（当前已实现）
- Backtest 层: 统一用 `BacktestError` 包装，记录后上抛

**涉及文件**: `pipeline.py`, `backtest/loop.py`

**工时**: 1 小时

---

### A6 — 数据库连接管理分散 🟡

**问题**: 多处 `sqlite3.connect()` 独立创建连接，无统一管理。

**方案**: 全项目通过 `DatabaseManager` 单例获取连接，禁止直接 `sqlite3.connect()`。

**涉及文件**: `repos/_base.py`, 所有直接 connect 的模块

**工时**: 3 小时

---

### A7 — Web 模块耦合到 pipeline 🟡

**问题**: `from web.state_broker import broker` 模块级导入，headless 环境需要 stub。

**方案**: 用延迟导入 + 条件判断:
```python
def _get_broker():
    try:
        from web.state_broker import broker
        return broker
    except ImportError:
        return None
```

**涉及文件**: `pipeline.py:35`

**工时**: 10 分钟

---

### A8 — Python 版本依赖未声明 🟡

**问题**: logger.py 需要 3.13+，但无 `python_requires` 约束。

**方案**: 在 `pyproject.toml` 添加 `python_requires = ">= 3.10"`，logger.py 的 `defaults` 参数改为兼容方案（见B8）。

**涉及文件**: `pyproject.toml`, `logger.py`

**工时**: 10 分钟

---

### A9 — 两个 excepthook 冲突 🟢

**问题**: `logger.py` 和 `excepthook.py` 都管理 `sys.excepthook`。

**方案**: 统一到 `excepthook.py`，logger.py 中移除 excepthook 相关代码。

**涉及文件**: `utils/logger.py`, `utils/excepthook.py`

**工时**: 1 小时

---

## 算法优化方案

---

### ALG1 — Quadratic Decay → Sigmoid Soft Cutoff (P1)

**方案**:
```python
# 当前 (有bug):
alpha[below] = alpha[below] * (alpha[below] / threshold) ** 2

# 修复后:
k = 10  # 陡度参数
alpha[below] = alpha[below] / (1 + np.exp(-k * (alpha[below] - threshold)))
```
**来源**: 报告建议 | **工时**: 1 小时

---

### ALG2 — IC 权重 Bayesian Shrinkage (P1)

**问题**: `IC权重 = abs(IC_IR)` 未考虑估计误差。

**方案**: `IC_bayes = (σ²_posterior * IC_prior + σ²_prior * IC_sample) / (σ²_prior + σ²_posterior)`

**来源**: Grinold & Kahn Eq. 6.16 | **工时**: 2 小时

---

### ALG3 — Kelly var 动态估算 (P1)

**方案**: `return_var = np.mean(np.diag(cov_matrix))` 从协方差矩阵动态计算。

**涉及文件**: `optimizer/kelly.py:76` | **工时**: 15 分钟

---

### ALG4 — Hierarchical Risk Parity (P2)

**方案**: 作为 Ledoit-Wolf 的对比方案，在 Small 层启用。使用 `scipy.cluster.hierarchy` + 递归二分权重。

**来源**: De Prado(2016) | **工时**: 3 小时

---

### ALG5 — Harvey 多重检验校正 (P2)

**方案**: 当因子数 > 50 时，IC t-statistic 阈值从 2.0 提升到 3.0+。

**来源**: Harvey, Liu & Zhu(2016) | **工时**: 1 小时

---

## 缺失功能实现方案

---

### FEAT1 — 端到端集成测试 (P0)

**方案**: 创建 `tests/test_e2e_20260722.py`:
1. 用 07-22 真实数据回放
2. 对比回测输出（信号/仓位/权益）与实盘结果
3. 断言 CAGR/MaxDD/Sharpe 在合理范围

**涉及文件**: `tests/`（新建）| **工时**: 4 小时

---

### FEAT2 — Almgren-Chriss 冲击成本模型 (P1)

**方案**:
- 实现 Almgren(2005) 线性冲击模型: `impact = σ * sqrt(T) * (η * X/VT + γ * sign(X))`
- 替代固定千一滑点
- 参数通过最小二乘校准（需 3 个月实盘数据）

**涉及文件**: `execution/impact.py`（扩展）| **工时**: 4 小时

---

### FEAT3 — 财报延迟建模 (P1)

**方案**: 引入 `report_publish_date` 字段，因子计算时用 `publish_date <= today` 替代 `stat_date <= today`。

**涉及文件**: `factor/compute/_dispatch.py`, `fundamental.py` | **工时**: 2 小时

---

### FEAT4 — BARRA 风格因子暴露 (P2)

**方案**: 实现 Growth/Value/Momentum/Size/Volatility 5 因子暴露计算，整合到 Brinson 归因报告中。

**涉及文件**: `monitor/attribution.py`（扩展）| **工时**: 3 小时

---

## 建议修复顺序

```
第 1 天 (P0):  B4→B6→B5→B8→B1→B3/L3→B2→L2→B7  (5h)
第 2 天 (P0+): B7 继续 + smoke test 全量回归
第 3 天 (P1):  B9-B23 (中等bug) + L4-L7 (3h)
第 4 天 (P2):  B24-B33 (轻微) (1.5h)
第 5 天:      架构 A7→A8→A9 (1h) + FEAT1 端到端测试 (4h)
第 6-7 天:    架构 A1→A2→A3 (9h)
第 8-10 天:   算法 ALG1-ALG5 (7.5h) + FEAT2-4 (9h)
```

---

*本方案基于 CODE_AUDIT_REPORT_2026-07-23.md，由逐文件完整阅读产生。所有建议待讨论确认后再落地。*
