# 参数审计报告 — 2026-07-05

全代码库硬编码参数审计, 检查是否具备文献/业界依据, 以及跨调用点一致性。

审计范围: 所有 .py 和 .sh 文件中出现在函数签名、SQL 常量、阈值比较中的数值参数。
排除: 测试文件、API URL 中的 ID、格式字符串、日志消息。

---

## 审计结果总览

| 类别 | 数量 | 占比 |
|------|------|------|
| 有依据 | 12 | 30% |
| 已修复 (本轮) | 10 | 25% |
| 工程经验值 (可接受) | 14 | 35% |
| 已修复 (本轮) | 10 | 25% |

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

### 1. ~~marginal.py:stepwise_selection — marginal_results 未定义~~ ✅ 已删除

位置: factor/marginal.py (已移除)
原因: 死代码, 从未被调用。功能已被 eval_stepwise.sh 的回测版 Layer 3 覆盖。
Grinold & Kahn 纯数学版步进筛选理论上可作快速预筛, 但未经验证 IC→IR 映射可靠性。
如需未来复用, 应基于实际回测数据验证后重写, 不应保留含 bug 的未验证代码。

### 2. ~~pipeline.py:286 — window=60 无注释~~ ✅ 已删除

协方差估计窗口 window=60 等于 covariance_matrix 函数默认值, 显式传参冗余且无注释。
已改为 covariance_matrix(log_ret, method="ledoit_wolf"), 默认值 60 的理由在函数定义处集中维护。

### 3. ~~backtest.py:51 — 注释与代码不一致~~ ✅ 已修复

根本问题: 注释误将回测区间长度说成"因子计算 lookback", 实际上 all_dates 是回测模拟区间的交易日,
与因子计算历史窗口完全独立 (因子计算数据由 pipeline 内部 get_daily 独立加载)。

修复:
- 阈值 65 → 250 交易日 (≈1年), 来源: Grinold & Kahn (1999) 60月≈250日, Lo (2002) SE(Sharpe)公式
- 注释改为准确描述回测统计意义, 删除对因子 lookback 的错误引用
- 附带修复 pipeline.py:161 start="2026-01-01" 硬编码 → 动态 365 日历日

### 4. ~~factor/compute.py — 多处 window=20 无注释~~ ✅ 已修复

根本问题: 9 个因子函数统一使用 window=20, 但各因子的文献推荐窗口不同.
compute_amihud 需要 250 (Amihud 2002: 12个月), compute_skewness 需要 60+
(Barberis & Huang 2008: SE(偏度)=√(6/N), N<30 时完全不可靠).
compute_ma_alignment 的 window=20 参数完全不参与计算 (MAs 硬编码), 属误导.

修复:
- compute_amihud: window 20 → 250 (Amihud 2002: 12个月日频)
- compute_skewness: window 20 → 60 (Barberis & Huang 2008: 日频等价)
- compute_ma_alignment: 删除未使用的 window 参数
- 全部 9 个因子窗口参数化至 config.yaml factor.windows (单一真相源)
- 每个窗口均有文献或业界注释

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


## 2026-07-05 终: 全量配置迁移

全代码库参数清点完成后, 15 个遗漏参数一次性迁入 config.yaml。

### 迁移清单

| 模块 | 参数 | yaml 路径 | 旧值 | 类型 |
|------|------|----------|------|------|
| backtest | capital | backtest.default_capital | 5000 | 业务参数 |
| backtest | start_date | backtest.default_start | 2026-01-01 | 默认值 |
| backtest | end_date | backtest.default_end | 2026-06-30 | 默认值 |
| backtest | rebalance interval | backtest.rebalance_interval_days | 5 | 策略参数 |
| backtest | lot_size | backtest.lot_size | 100 | 交易所规则 |
| pipeline | industry min | risk.neutralization.industry_min_count | 30 | 统计门禁 |
| store | stale_days | data.stale_days | 250 | 数据时效 |
| store | batch_size | data.batch_size | 50 | 工程优化 |
| store | gap_fill_limit | data.gap_fill_limit | 100 | 批量上限 |
| store | PE max | data.pe_max | 1000 | 数据清洗 |
| store | derived ratio max | data.derived_ratio_max | 100 | 数据清洗 |
| stats_cache | snapshot TTL | factor.stats.snapshot_ttl_sec | 86400 | 缓存策略 |
| stats_cache | IC min periods | factor.stats.ic_min_periods | 20 | 统计门禁 |
| compute | zscore min | factor.compute.zscore_min_count | 30 | 统计门禁 |
| covariance | window | risk.covariance.window | 60 | 协方差估计 |
| covariance | min_periods | risk.covariance.min_periods | 30 | 最少数据量 |

### 不入 yaml 的常量 (确认)

| 常量 | 原因 |
|------|------|
| LOT_SIZE=100 (去重后) | 已在 backtest.lot_size |
| MINUTE=60 | 物理常数 |
| MAX_SYMBOLS=900 | SQLite SQLITE_MAX_VARIABLE_NUMBER=999 |
| sqrt(252) | 年化交易日国际标准 |
| 统计公式 (sqrt(6/N) 等) | 数学恒等式 |
| API 固定参数 (datalen=2000, scale=240) | 外部服务限制 |

### 配置结构总览

config.yaml 现已覆盖 7 个领域、50+ 配置项:
- alpha (方法/窗口/阈值)
- risk (协方差/中性化/约束)
- execution (费率/滑点)
- optimizer (方法/频率/上限)
- backtest (基准/区间/调仓/门槛)
- data (来源/时效/清洗/批量)
- factor (窗口/评估/统计/缓存)
- web (端口)
- cache (Redis)

单一真相源原则: 所有代码通过 cfg("path", fallback) 读取, fallback 仅作防御.
