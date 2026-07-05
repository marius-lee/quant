# 参数审计报告 — 2026-07-05

全代码库硬编码参数审计, 检查是否具备文献/业界依据, 以及跨调用点一致性。

审计范围: 所有 .py 和 .sh 文件中出现在函数签名、SQL 常量、阈值比较中的数值参数。
排除: 测试文件、API URL 中的 ID、格式字符串、日志消息。

---

## 审计结果总览

| 类别 | 数量 | 占比 |
|------|------|------|
| 有依据 | 12 | 30% |
| 已修复 (本轮) | 6 | 15% |
| 工程经验值 (可接受) | 18 | 45% |
| 待修复 | 4 | 10% |

---

## 有依据的参数 (保留)

| 参数 | 位置 | 值 | 依据 |
|------|------|------|------|
| min_count | factor/compute.py:_cs_zscore | 30 | 中心极限定理 n>=30 |
| top_fraction | factor/intersection.py | 0.20 | Fama-French 五分位组合标准 |
| top_frac (soft_truncation) | pipeline.py | 0.30 | Grinold & Kahn: alpha 覆盖前 20-30% |
| stop_loss_pct | 配置默认 | 0.15 | 权益多空策略行业默认 |
| MAX_SYMBOLS | data/store.py | 900 | SQLite SQLITE_MAX_VARIABLE_NUMBER=999 |
| sqrt(252) | backtest.py (3处) | 252 | 年化交易日标准 |
| LOT_SIZE | backtest.py | 100 | 中国A股1手=100股 |
| calls_per_minute (tushare) | data/store.py | 200 | tushare pro 免费版限频 |
| calls_per_minute (akshare) | data/store.py | 60 | akshare 东方财富 API 经验限频 |
| TTL (stock_list/industry) | data/store.py | 24h | 股票列表日更, 24h 缓存合理 |
| _SNAPSHOT_TTL_SEC | factor/stats_cache.py | 86400 | 24h 过期标准 |
| PE > 1000 过滤 | data/store.py | 1000 | 极端值噪声过滤, 无 alpha 价值 |

---

## 本轮已修复

| 参数 | 旧值 | 新值 | 依据 |
|------|------|------|------|
| n_symbols (stats_cache) | 200/300/500 | 800 | 中证800 量化标准基准 |
| n_symbols (eval_stepwise) | 300 | 800 | 同上 |
| lookback (stats_cache) | 90/120 | 120 | 券商研报惯例, t=|IR|*sqrt(n) |
| lookback (eval_stepwise) | 90 | 120 | 同上 |
| n_days (marginal.py) | 90 | 120 | 同上 |
| SQL '-120 days' | 硬编码 | 参数化 | 从 lookback 派生 |

详细论证见 ADR 007 2026-07-05 补遗。

---

## 工程经验值 (可接受, 但建议加注释)

| 参数 | 位置 | 值 | 理由 | 建议 |
|------|------|------|------|------|
| window=60 | pipeline.py:covariance_matrix | 60 | Ledoit-Wolf 协方差估计滚动窗口, 60天约为3个月 | 加注释 |
| window=20 | factor/compute.py (5处) | 20 | 日线因子计算窗口, 文献标准 20-60 日 | 已有文献引用, 可接受 |
| batch_size=50 | data/store.py | 50 | TickFlow 批量拉取效率最佳值 | 已注释 OK |
| datalen=2000 | data/store.py:sina | 2000 | 新浪 API 返回上限 | 加注释 |
| IC decay horizons [1,5,20] | factor/stats_cache.py | 1/5/20 | 标准短期/周度/月度前瞻 | OK |
| HAVING COUNT(*) >= 60 (旧) | factor/stats_cache.py | 60 (旧) | 已改为 lookback//2 | 已修复 |
| corr min_count=30 | factor/stats_cache.py | 30 | 截面相关性最少共同样本 | 加注释 |
| IC min_periods=20 | factor/stats_cache.py | 20 | IC 最少截面数 | 加注释 |
| backtest min_dates=65 | backtest.py | 65 | 注释说60用65 (可能是60+5缓冲) | 统一注释与代码 |
| limit=100 | data/store.py:gap_fill | 100 | 单次拉取上限防超时 | OK |
| stale_days=250 | data/store.py | 250 | 一年未更新视为过期 | OK |
| capital=5000 | backtest.py | 5000 | 回测初始资金 (元) | 可配置 |
| period='2026-01-01 to 2026-06-30' | backtest.py | 半年 | 回测默认区间 | 应外置为参数 |
| MAX_ITERS=20 | factor/marginal.py | 20 | 步进选因子上限 | OK |
| threshold=1.0 - top_fraction | factor/intersection.py | 派生 | 从 top_fraction 计算 | OK |

---

## 待修复

### 1. marginal.py:stepwise_selection — marginal_results 未定义

位置: factor/marginal.py:205
marginal_results 是 compute_marginal_evaluation 的局部变量, stepwise_selection 无法访问。
影响: 函数一旦被调用会抛 NameError。目前无调用者 (死代码)。
建议: 重构为从 ranked_candidates 中的 result dict 提取。

### 2. pipeline.py:286 — window=60 无注释

covariance_matrix(log_ret, method="ledoit_wolf", window=60) — 60 天窗口无注释说明选择原因。
建议: 加注释说明 60 trading days standard for daily cov estimation.

### 3. backtest.py:51 — 注释与代码不一致

注释说 60 天, 代码用 65。原因不明 (可能是 60 + 5 天缓冲)。
建议: 统一为 60 或更新注释。

### 4. factor/compute.py — 多处 window=20 无注释

5 处函数使用 window=20 作为默认窗口。
文献引用在文件头部, 但与具体值之间的映射不清晰。
建议: 每个 window: int = 20 后加简短注释。

---

## 审计后的一致性状态

| 参数 | 旧: 各调用点值 | 新: 统一值 |
|------|----------------|-----------|
| n_symbols | 200, 300, 500, 500 | 800 |
| lookback / n_days | 90, 90, 120 | 120 |
| 选股 SQL 窗口 | 硬编码 120 | 参数化 lookback * 1.5 |

---

## 规则建议

为避免未来再次出现随手参数, 建议在 coding-standards 中增加检查项:

数值参数检查: 所有函数签名中的数值默认值、SQL 常量、硬编码阈值必须:
1. 有内联注释说明依据 (文献/业界标准/经验测试结果)
2. 如无依据, 标注 FIXME: 需确定依据 并记录到本文档
