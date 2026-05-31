# Quant 项目全面审计报告

**审计日期**：2026-05-31 | **审计范围**：34 个 Python 文件，~5000 行代码 | **审计人**：Claude (系统架构师)

---

## 目录

1. [已实现功能总结](#一已实现功能总结)
2. [问题总览](#二问题总览)
3. [致命问题 (CRITICAL) — 9个](#三致命问题-critical)
4. [高优先级 (HIGH) — 15个](#四高优先级-high)
5. [中优先级 (MEDIUM) — 28个](#五中优先级-medium)
6. [低优先级 (LOW) — 43个](#六低优先级-low)
7. [架构评估](#七架构评估)
8. [缺失功能清单](#八缺失功能清单)
9. [配置审计](#九配置审计)
10. [建议行动路线](#十建议行动路线)

---

## 一、已实现功能总结

| 子系统 | 已实现 | 状态 |
|--------|--------|------|
| **数据层** | SQLite (market.db)，全A股~5500只，日线 OHLCV，PE/PB/市值 (腾讯财经)，增量更新 | ✅ 完整 |
| **数据源** | tushare (日线+基本面) + akshare (股票列表) + 腾讯财经 (PE/PB/市值) | ⚠️ baostock 未实现 |
| **因子层** | 52 手工因子 (技术20 + 博弈论28 + 代理基本面4) + 真实基本面8 + auto-alpha 20 | ⚠️ 有3个致命bug |
| **因子缓存** | SQLite factors_cache，增量更新，YYYMMDD 日期比较 | ⚠️ 重复键崩溃风险 |
| **IC 筛选** | 分块精确 IC (充分统计量累加)，向量化 3.8s | ⚠️ 前视偏差 |
| **模型训练** | 5模型集成 (LightGBM+XGBoost+RF+ET+Ridge)，abs(IC) 加权，504天窗口 | ✅ |
| **预测+排名** | 分批预测 → demon信号混合 → 行业市值中性化 | ⚠️ demon因子有重复 |
| **回测** | 向量化回测 (pandas秒出) + 事件驱动引擎 (保留未用) | ⚠️ 静态持仓无再平衡 |
| **Web UI** | Flask 8521，ECharts K线图，推荐列表，回测指标，行业分布 | ✅ |
| **定时任务** | launchd 每天 8:00+16:00，auto_run.py，告警通知 | ✅ |
| **日志** | 双输出 (控制台+滚动文件)，quant 命名空间 | ✅ |

---

## 二、问题总览

**共 95 个发现**：🔴 致命 9 个，🟠 高 15 个，🟡 中 28 个，🟢 低 43 个。

---

## 三、致命问题 (CRITICAL) — 必须立即修复

### C1: `engine/screener.py:17` — 因子 IC 筛选中的前视偏差

**描述**：`future_5d = close_df.pct_change(5).shift(-5)` 计算前向收益时跨越训练/测试集边界。训练集最后 ~5 个日期的前向收益使用了测试期价格数据。

**生产影响**：高估因子预测能力，选出的因子含未来信息泄露，回测结果不可信。

**修复方案**：排除前向收益与测试期有重叠的训练日期：
```python
split_idx_clean = max(0, split_idx - 5)
train_dates_set = set(all_dates[:split_idx_clean])
```

---

### C2: `factor/real_fundamental.py:23` — 全量重建时的前视偏差

**描述**：`price = close_df.iloc[-1]` 取最新收盘价，全量重建缓存时（如从 2020-01-01 起），将 2026 年的 PE/PB/市值 广播填充到 2020 年的所有历史日期。

**生产影响**：若触发全量重建，历史回测指标完全无效。

**修复方案**：
1. 在 `cache.py` 中全量重建模式下，按日期逐日计算真实基本面因子（而非用最新价广播）
2. 或者在全量重建时跳过真实基本面因子，仅增量更新时使用
3. 最少改动：在 `cache.py` 中检测全量重建模式，跳过 `compute_real_factors`，待首次增量更新时再填充

---

### C3: `factor/game_theory.py:30,33` — PIN proxy 恒为 1.0

**描述**：
```python
ov_vol = ((close / close.shift(1) - 1).rolling(w).std())
tv_vol = ret.rolling(w).std()
```
`ov_vol` 和 `tv_vol` 计算完全相同的量（日收益率波动率），因此 `ov_vol / tv_vol ≡ 1.0`。

**生产影响**：这是死因子 — 贡献零信号，浪费计算，且不提供任何排序信息。

**修复方案**：PIN proxy 的分母应为隔夜波动率（开盘价/前收盘价-1），分子为日间波动率：
```python
ov_vol = ((data.get("open") / close.shift(1) - 1).rolling(w).std())
tv_vol = ret.rolling(w).std()
```
若无开盘价数据，则移除此因子。

---

### C4: `factor/game_theory.py:37` — 4 个 info_arrival 因子完全相同

**描述**：
```python
for w in self.windows:
    ...
    (f"info_arrival_{w}d", (V / V.rolling(max(self.windows)*5, min_periods=1).mean()).clip(0, 10)),
```
循环内忽略窗口参数 `w`，固定使用 `rolling(300)`。产出 `info_arrival_5d`, `_10d`, `_20d`, `_60d` 四个完全相同的因子。

**生产影响**：引入完美共线性，拖累模型训练质量。

**修复方案**：将 `max(self.windows)*5` 改为 `w*5`，或将此因子移出循环作为单个因子 `info_arrival`。

---

### C5: `factor/cache.py:97` — 增量更新中断后重跑触发 IntegrityError

**描述**：`stacked.to_sql("factors_cache", conn, if_exists="append", ...)` 使用普通 INSERT，但表有 `UNIQUE INDEX ON (stock, date)`。增量更新中断后重跑，已存在的日期-股票组合触发 `IntegrityError`。

**生产影响**：管线崩溃，缓存无法恢复。

**修复方案**：
```python
if mode == "append":
    dates = stacked["date"].unique()
    for d in dates:
        conn.execute("DELETE FROM factors_cache WHERE date = ?", (d,))
    conn.commit()
stacked.to_sql("factors_cache", conn, if_exists="append", ...)
```

---

### C6: `data/fundamental.py:23` — WAL 模式被绕过

**描述**：`sync_all()` 直接用 `sqlite3.connect(db_path)` 而非 `DataStore._connect()`（后者设了 `PRAGMA journal_mode=WAL`）。

**生产影响**：与 WAL 模式连接并发时 "database is locked"。

**修复方案**：统一路由所有连接通过 `DataStore._connect()`，或在 `sync_all()` 入口复制 WAL pragma：
```python
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
```

---

### C7: `strategy/signals.py:40` — 除零崩溃

**描述**：
```python
pos_pred = predictions[long_mask].clip(lower=0)
weights[long_mask] = pos_pred / pos_pred.sum()  # 全部为负时 sum=0
```

**修复方案**：
```python
total = pos_pred.sum()
if total > 0:
    weights[long_mask] = pos_pred / total
else:
    weights[long_mask] = 1.0 / long_mask.sum()
```

---

### C8: `web/db.py:36` — NumPy 类型 JSON 序列化崩溃

**描述**：`json.dumps(result)` 不能序列化 `numpy.float64/int64/bool_` 类型。分析成功完成，但结果写入 `results.db` 时静默失败。

**修复方案**：添加递归转换函数：
```python
def _to_native(obj):
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(v) for v in obj]
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.bool_,)): return bool(obj)
    if isinstance(obj, (pd.Timestamp,)): return str(obj)
    return obj
```

---

### C9: `config/config.yaml:5` + `data/store.py:279` + `backtest/event_engine.py:197` — API Token 硬编码泄露

**描述**：Tushare API token `aeeb8c7d...` 明文存于 3 个文件。

**修复方案**：
- `config.yaml`：移除 token，用注释说明从环境变量读取 `TUSHARE_TOKEN`
- `store.py __main__`：改为 `os.environ.get("TUSHARE_TOKEN", "")`
- `event_engine.py __main__`：同上

---

## 四、高优先级 (HIGH) — 15 个

### H1: `factor/base.py:24` — normalize_zscore 截面 std=0 时整行变 NaN

当某日所有股票因子值相同时（如新股、常量回退值），`std=0` → 整行 `0/0 = NaN`。

**修复**：`std = std.replace(0, 1.0)`

---

### H2: `factor/screening.py:141` — O(N) Python 列表推导 + 可能 KeyError

```python
train_idx = [idx for idx in chunk_factors.index if idx[0] in train_dates_set]
chunk_y = y_stacked.loc[train_idx]  # KeyError on missing (date, stock)
```

**修复**：使用向量化操作 + 取交集：
```python
date_mask = chunk_factors.index.get_level_values(0).isin(train_dates_set)
common_idx = chunk_factors.index[date_mask].intersection(y_stacked.index)
```

---

### H3: `factor/alpha_factory.py:128` — 回退逻辑选错候选

注释说"取 IC 绝对值最大的 5 个"，但代码遍历 `list(candidates.items())[:50]`（插入顺序，非 IC 排序）。

**修复**：按 IC 排序后取前 `n_keep`：
```python
sorted_candidates = sorted(candidates.items(), key=lambda x: abs(x[1]), reverse=True)
for name, ic in sorted_candidates[:n_keep]:
    ...
```

---

### H4: `factor/fundamental.py:33` — size_proxy 用交易量当市值代理

`np.log(close.abs() * volume + 1)` 测量的是"日成交金额"而非"公司市值"。`volume` 是当日成交量，波动极大。

**修复**：改名为 `dollar_volume`，或从基本面数据获取真实市值。

---

### H5: `engine/predictor.py:20` — 批次间"最新日期"不一致

退市股最后数据日期早，新股历史短 → 各批次取到不同"最新日期" → 预测结果混合不同时间点快照。

**修复**：在循环前统一确定最新日期，然后只加载该日期的因子数据。

---

### H6: `engine/predictor.py:16` — 每批加载全部历史只用最新1天

`factors_repo.load_batch(chunk)` 无日期参数 → 加载 ~1422 天/股票。只用最新 1 天。内存浪费 ~1000x。

**修复**：新增 `load_latest(symbols)` 方法，或给 `load_batch` 传日期参数：
```python
latest = factors_repo.max_date()
batch_factors = factors_repo.load_batch(chunk, start_date=latest, end_date=latest)
```

---

### H7: `engine/predictor.py:25` — model 为 None 时无保护

`trainer.train_model()` 可能返回 `None`（数据不足），但 `predict_all()` 不检查。

**修复**：函数入口加守卫：
```python
if model is None:
    logger.warning("predict_all: model is None, cannot predict")
    return pd.Series()
```

---

### H8: `engine/backtest_runner.py:46-47` — 基准缺失日用 0 填充

`aligned_bench = bench_returns.reindex(daily_return.index).fillna(0)` 在基准缺失日用 0 收益率填充 → 人为抬高 alpha。

**修复**：用 `dropna()` 丢弃缺失日：
```python
aligned_bench = bench_returns.reindex(daily_return.index).dropna()
common_idx = daily_return.index.intersection(aligned_bench.index)
```

---

### H9: `data/store.py:130-194` — update_daily 宣称增量但每次重拉全部历史

每次都从 `"20200101"` 开始拉取 → 浪费 tushare 免费版每日配额（~200次/天）。

**修复**：拉取前先查询 `SELECT MAX(date) FROM daily WHERE symbol=?`，只拉新日期。

---

### H10: `data/repository.py:86-101` — YYYY-MM-DD 与 YYYYMMDD 日期格式比对失败

`factors_cache.date` 存为 `YYYYMMDD`（或 `YYYY-MM-DD`，取决于来源）。字符串比较 `"2024-01-01" < "20240101"` 在位置7因为 `"0" < "-"` 导致静默返回零行。

**修复**：查询前统一日期格式：
```python
if start_date:
    start_date = start_date.replace("-", "") if len(start_date) == 10 else start_date
```

---

### H11: `web/app.py:61,78` — _analysis_running 无锁

并发 POST `/api/run` 可突破互斥，两个线程同时运行 `RecommendationEngine.run()` → 数据库锁冲突、结果损坏。

**修复**：添加 `threading.Lock()` 保护。

---

### H12: `auto_run.py:51-62` — 同步步骤失败后状态不一致

`sync_stock_list()` 成功但 `update_daily()` 失败 → 下次运行时阈值已满足 → 跳过修复 → 永久不一致。

**修复**：每个同步步骤独立 try/except，失败后记录状态并在下次运行时重试。阈值检查改为基于"上次成功时间"而非行数。

---

### H13: `data/store.py:180-182` — 550 批次中只记录前 3 个失败

```python
if i < 3: logger.warning(...)
```
批次 4-549 失败完全静默。

**修复**：独立计数失败，末尾汇总：`logger.error(f"daily update: {fail_count}/{total} batches failed")`

---

### H14: `strategy/ensemble.py:107-114` — 任一模型 NaN 则集成全 NaN

无 `np.nan_to_num` 校验，某模型产 NaN → 加权求和全 NaN。

**修复**：
```python
p = model.predict(X)
if np.any(np.isnan(p)):
    logger.warning(f"NaN predictions from {name}")
    p = np.nan_to_num(p, nan=0.0)
```

---

### H15: `backtest/metrics.py:64` — "Alpha" 标签错误

`alpha = excess.mean() * 252` 是年化超额收益 (active return)，不是 Jensen's Alpha（后者减去 `beta * (r_b - r_f)`）。

**修复**：改名为 `active_return` 或实现真实 Jensen's Alpha。

---

## 五、中优先级 (MEDIUM) — 28 个

### M1: `engine/screener.py:21` — 训练/测试分割比例硬编码
`split_idx = int(len(all_dates) * 0.7)` — 不可配置。

**修复**：从 config 读取 `cfg("strategy.train_split", 0.7)`

### M2: `engine/ranker.py:32` — ML/Demon 混合权重硬编码
`combined = ml_norm * 0.5 + latest_demon_clean * 0.5`

**修复**：从 config 读取 `cfg("ranker.ml_weight", 0.5)`

### M3: `engine/ranker.py:55-58` — 行业哑变量编码：单股票行业被中性化抹零
不设截距项时，仅含1只股票的行业其哑变量系数完全吸收得分 → 残差=0。

**修复**：移除仅含1只股票的行业，或加入截距项。

### M4: `engine/builder.py:54` — 5日涨跌用求和而非复利
`perf.iloc[-5:].sum() * 100`

**修复**：`((1 + perf.iloc[-5:]).prod() - 1) * 100`

### M5: `engine/backtest_runner.py:29` — 静态持仓无再平衡
在整个测试期持有期初的 top N 股票，无任何调仓。文档中声称每月再平衡。

**修复**：实现滚动前向预测（每个测试日期重新训练/预测），或至少分段回测。

### M6: `data/repository.py:22-28` — ST 过滤依赖名称含"ST"
```sql
WHERE s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
```
不存 `list_status` 字段，名称更新滞后。

**修复**：`sync_stock_list` 时存储 `list_status`，用 `list_status='L'` 过滤。

### M7: `data/store.py:172` — turnover 字段永写 0.0
tushare `pro.daily()` 默认字段不含 turnover。列的默认值永远不被更新。

**修复**：要么从补充 API 获取换手率，要么移除此列。

### M8: `factor/base.py:47` — Double-transpose fillna 性能差
`factor_df.T.fillna(factor_df.median(axis=1)).T` 在大 DataFrame 上很重。

**修复**：使用 `factor_df.apply(lambda col: col.fillna(factor_df.median(axis=1)))` 或 pandas 2.1+ 的 `axis=0` fillna。

### M9: `factor/game_theory.py docstring` — 宣称 32 因子实际 28
Docstring 说 "8类x4窗口=32因子" 但只定义了 7 对（28个因子）。

**修复**：更新 docstring 或补全缺失因子。

### M10: `factor/game_theory.py:34` — CSSD 用 MAD 而非 RMS
`dev.rolling(w).mean()` 计算均值绝对偏差，而非标准差。CSSD 的标准定义是 RMS。

**修复**：改名为 `herding_csmad`，或在文档中说明使用 MAD 的理由。

### M11: `factor/demon.py:39` — rolling max 包含当前日
`vol_ratio.rolling(surge_window).max()` 包含当日的量比信息。日末计算安全，但盘中预测会有前视偏差。

**修复**：如仅用于日末则加注释说明；如用于盘中则 `.shift(1).rolling(...)`。

### M12: `factor/cache.py:82-89` — 不对称错误处理
Alpha factory 个别失败有 try/except 保护，但 `TechnicalFactors`, `GameTheoryFactors` 等不保护。

**修复**：所有因子类别加 try/except，单一类别失败不应阻止其他类别。

### M13: `factor/cache.py:101` — 日志间隔误导
`if i % 500 == 0` 但 `batch_size=200`，实际每 5 批（1000只）才 log 一次。

**修复**：改为 `if i % 1000 == 0` 或用 `(i // batch_size) % 5 == 0`。

### M14: `factor/fundamental.py:49-51` — 列顺序依赖 Python dict 插入顺序
`pd.concat` 的结果列序依赖 dict 插入顺序，`pd.MultiIndex.from_product` 也依赖此顺序。

**修复**：显式构建 MultiIndex，不依赖隐式顺序。

### M15: `factor/real_fundamental.py:43-46` — price.get(sym, 0) 默认 0 不正确
股票缺失时默认价格为 0 → `(0 - low52) / rng = -1` → clip 到 0 → 52周位置为 0（实际可能是任意位置）。

**修复**：缺失股票跳过而非默认 0。

### M16: `factor/alpha_factory.py:36-37` — _ts_corr 是死代码
`_ts_corr` 函数定义但从未被 `generate()` 的算子列表使用。

**修复**：移除或加入算子列表。

### M17: `factor/screening.py:52` — 冗余 boolean-index + dropna
两步可合并为一步。

### M18: `data/fundamental.py:91` — decode("gbk", errors="ignore") 静默损坏数据
解码错误时静默丢弃字节 → 可能输入错误数据。

**修复**：改用 `errors="replace"`，损坏数据无法通过 `_is_number`。

### M19: `data/fundamental.py:120-126` — 连续3批次失败中止全部剩余
网络抖动后 40% 的基本面数据永久缺失。

**修复**：独立计数 total_fails，超过 20% 时才中止。

### M20: `data/fundamental.py:110` — 死代码行
`if "total_mv" in vals: pass  # 已是亿` — 空操作。

**修复**：改用注释。

### M21: `data/fundamental.py:112-115` — UPDATE 静默影响零行
如果腾讯返回的 symbol 不在 stocks 表中，UPDATE 不影响任何行且无警告。

**修复**：检查 `cursor.rowcount`。

### M22: `data/store.py:56-59` — WAL PRAGMA 未验证
不检查 `journal_mode=WAL` 是否实际生效。

**修复**：`result = conn.execute("PRAGMA journal_mode").fetchone()[0]`，与期望值比较。

### M23: `data/store.py:207-212` — get_daily 无变量限制分块
5500 个 `?` 占位符超出旧版 SQLite 999 限制。

**修复**：仿照 `FactorRepo.load_batch` 做分块。

### M24: `data/store.py:260` — get_benchmark 硬编码 .SH 后缀
`ts_code=f"{code}.SH"` 对深交所指数（如 399006）产生错误代码。

**修复**：根据代码前缀判断交易所：6xx → SH，0xx/3xx → SZ。

### M25: `data/store.py:98 vs 120` — sync_stock_list 返回值不一致
tushare 路径返回 `total`（总股票数），akshare 路径返回 `new_count`（新增数）。

### M26: `backtest/metrics.py:28` — 超短期年化无意义
2天回测：`years = 2/252 ≈ 0.008`，1% 收益 → `(1.01)^100 - 1 ≈ 170%` 年化。

**修复**：设置最小年数阈值（如 0.25 年），低于阈值返回 None + warning。

### M27: `backtest/event_engine.py:86-89` — 无成交量数据时默认约束不切实际
默认 `1e9` 股上限 → 实际意味无限。

### M28: `backtest/event_engine.py:169-175` — T+1 买入日净值低估
`_total_value()` 排除 `pending_buys`，买入日资产被低估约 0.13%。

### M29: `strategy/ensemble.py:84-85` — 单次迭代 clip-normalize 不保证上限
Clip+renormalize 后某些权重可能重新突破上限。

---

## 六、低优先级 (LOW) — 43 个

### L1-L8: 硬编码魔数
| 值 | 文件:行 | 用途 |
|------|---------|------|
| `0.7` | screener.py:21 | 训练/测试分割 |
| `504` | trainer.py:19 | 训练窗口（后备值）|
| `500` | predictor.py:10 | 预测批次大小 |
| `0.5/0.5` | ranker.py:32 | ML/妖股权重 |
| `30` | backtest_runner.py:22 | 最大持仓（后备值）|
| `20` | builder.py:16 | 展示 top N（后备值）|
| `0.01/0.05` | screener.py:27 | IC 阈值 |
| `"000300"` | backtest_runner.py:43 | 基准代码 |

### L9: `engine/loader.py:18-23` — 返回字典键不一致
成功 `{all_stocks, close_df}`，失败 `{error, all_stocks}`。

### L10: `engine/ranker.py:29-30` — 全量 min-max 归一化掩盖离群值
Top 1% 和 Top 10% 归一化后得分相近。

### L11: `engine/ranker.py:62` — lstsq 在 130+ 行业哑变量时数值不稳定

### L12: `engine/backtest_runner.py:35` — 测试期不存在的股票静默丢弃

### L13: `engine/builder.py:62` — get_industry_mv 加载了市值但只用行业

### L14: `engine/builder.py:76` — 假设 close_df.index 为 DatetimeIndex

### L15: `engine/predictor.py:28` — 异常被吞没无 exc_info

### L16: `engine/predictor.py:24` — 所有因子不可用时静默跳过

### L17: `engine/trainer.py:33-34` — train_mask 冗余（train_factors 已限定日期范围）

### L18: `engine/trainer.py:22-23` — 训练窗口外数据静默丢弃

### L19: `factor/technical.py:22-60` — 4 个独立的 for w in self.windows 循环

### L20: `factor/fundamental.py:33` — close.abs() 无必要（股价永正）

### L21: `factor/alpha_factory.py:58` — momentum 和 vol_ratio 算子与基础算子重叠

### L22: `factor/cache.py:55` — 日期格式转换脆弱

### L23: `web/app.py:128-130` — 绕过 DataStore 直接连接 SQLite

### L24: `web/app.py:126` — kline endpoint 不对 symbol 做格式验证

### L25: `web/app.py:117` — track endpoint 语义模糊（无数据 vs 零变化）

### L26: `web/app.py:22-34` — 懒加载单例非线程安全

### L27: `web/db.py:29` — save_result 无 try/except

### L28: `web/db.py:41-43` — picks 插入 KeyError 风险

### L29: `web/db.py:12-13` — 数据库连接未关闭

### L30: `utils/logger.py:15-22` — 懒初始化竞态条件

### L31: `auto_run.py:90` — top_score 阈值硬编码 0.005

### L32: `auto_run.py:19` — _get_prev_picks 绕过 db.py 直连 SQLite

### L33: `config/loader.py:10-17` — load() 无锁

### L34: `strategy/signals.py:8` — __main__ 缺少 numpy import

### L35: `strategy/ensemble.py:74` — Ridge(random_state=42) 对默认 solver 无效

### L36: `data/fundamental.py:40-45` — try/except 做控制流（ALTER TABLE ADD COLUMN）

### L37: `store.py:86-89` — 交易所映射写死字符串

### L38: `store.py:277-280` — __main__ 中硬编码 token

---

## 七、架构评估

### 评分：B+ (良好，有明确改进路径)

**优势**：
- 分层清晰 (`config/utils` → `data` → `factor` → `strategy` → `backtest` → `engine` → `web`)，无循环依赖
- 内存控制设计用心（三层保护：不预加载/分块 IC/504 天窗口）
- SQLite 分块策略正确（900 参数限制）
- 配置系统设计合理（点号路径），只是未充分激活
- 因子缓存增量更新设计合理

**核心架构问题**：

1. **文档与实现脱节** — README 宣称事件驱动回测是主要方法，但生产管线用无成本、无滑点、无风控的向量化回测。报告 Sharpe 1.24 在不含交易成本的静态持仓假设下——与真实交易不可比。

2. **配置系统严重低利用率 (48% 死配置)** — `data.universe`, `data.frequency`, `strategy.target`, `strategy.model` 等关键键定义但从未读取。操作者修改后不会产生任何行为变化。

3. **日期格式碎片化** — YYYYMMDD 字符串 vs `pd.Timestamp` vs YYYY-MM-DD 三种格式在模块间来回转换，容易滋生 bug。

4. **零测试** — 整个项目没有一个单元测试。

5. **缺失功能链** — 无幸存者偏差校正、无企业行动处理、无涨跌停限制、无策略对比框架、无再平衡逻辑。

---

## 八、缺失功能清单

### P0 — 影响指标可信度

| 功能 | 说明 |
|------|------|
| 修复前视偏差 | screener.py 和 real_fundamental.py 两处数据泄露 |
| 事件驱动回测集成 | 或在向量化回测中加入交易成本 + 再平衡 |
| 样本外验证框架 | 滚动窗口交叉验证替代单次 7:3 拆分 |
| 幸存者偏差校正 | 按历史时点筛选股票（当时在市的股票）|

### P1 — 生产系统必需

| 功能 | 说明 |
|------|------|
| 再平衡逻辑 | 至少每月调仓，而非静态买入持有 |
| 交易成本模型 | 手续费+滑点+涨跌停约束 |
| 数据质量验证 | 训练前检查 NaN 比例、日期连续性、数据停滞 |
| 主动告警 | 管线失败时邮件/webhook/弹窗通知 |
| 配置激活 | 连接 11 个死配置键或删除 |
| 日期格式统一 | 选定一种格式全系统使用 |

### P2 — 功能完善

| 功能 | 说明 |
|------|------|
| baostock 数据源 | README 宣称但未实现 |
| 单元测试 | 至少冒烟测试 + 核心计算验证 |
| 策略对比框架 | 多策略并行 + 指标排名 |
| 企业行动处理 | 分红复权、拆股调整 |
| Web 身份验证 | 本地使用可接受，外网暴露需要 |

---

## 九、配置审计

### 已定义但从未读取的配置键 (11/23 = 48% 死配置)

| 键 | 判定 |
|---|---|
| `data.universe: "hs300"` | 未使用 — 股票池未被过滤 |
| `data.frequency: "daily"` | 未使用 — 硬编码为每日 |
| `data.start_date: "2020-01-01"` | 未使用 — 硬编码 `"20200101"` 在 5 个位置 |
| `data.cache_dir: "./data/cache"` | 未使用 — 路径硬编码 |
| `factor.use_technical: true` | 未使用 — 始终计算所有因子类别 |
| `factor.use_fundamental: true` | 未使用 |
| `factor.na_fill: "median"` | 未使用 — BaseFactor 硬编码默认值 |
| `factor.normalize: "zscore"` | 未使用 — normal_zcore 硬编码 |
| `factor.winsorize: "mad"` | 未使用 — winsorize_mad 硬编码 |
| `strategy.model: "lightgbm"` | 未使用 — EnsembleModel 始终运行 5 模型 |
| `strategy.target: "return_5d"` | 未使用 — 5d 硬编码在 screener.py 和 alpha_factory.py |

### 已读取的键 (6/23 = 26%)
`data.tushare_token`, `strategy.train_window`, `backtest.initial_capital`, `backtest.max_positions`, `backtest.benchmark`

### 仅被未使用的事件引擎读取的键 (6/23 = 26%)
`backtest.commission`, `backtest.slippage`, `backtest.max_weight`, `risk.max_drawdown`, `risk.max_sector_exposure`, `risk.daily_loss_limit`

---

## 十、建议行动路线

### 阶段一：立即修复（致命问题，1-2天）
修复 C1-C9：
- 前视偏差 x2 (screener.py + real_fundamental.py/cache.py)
- PIN proxy 死因子 (game_theory.py)
- info_arrival 重复因子 (game_theory.py)
- 缓存重复写入崩溃 (cache.py)
- WAL 模式绕过 (fundamental.py)
- 除零崩溃 (signals.py)
- JSON 序列化 (db.py)
- Token 泄露 (config.yaml + store.py + event_engine.py)

### 阶段二：本周修复（高优问题，3-5天）
修复 H1-H15：
- normalize_zscore NaN (base.py)
- 筛选性能 + KeyError (screening.py)
- alpha_factory 回退逻辑 (alpha_factory.py)
- size_proxy 名称/实现 (fundamental.py)
- predictor 日期一致性和内存浪费 (predictor.py + repository.py)
- model None 检查 (predictor.py)
- 基准填充错误 (backtest_runner.py)
- update_daily 增量修复 (store.py)
- 日期格式统一 (repository.py)
- 并发锁 (app.py)
- 同步状态一致性 (auto_run.py)
- 批量失败日志 (store.py)
- 集成 NaN 保护 (ensemble.py)
- Alpha 标签修正 (metrics.py)

### 阶段三：下次迭代（中优问题，1-2周）
修复 M1-M29 中高影响的 15 个：
- 硬编码参数可配置化
- 回测加入再平衡
- ST 过滤改用 list_status
- 行业中性化修复
- 5 日涨跌复利
- 配置键激活

### 阶段四：中期规划（2-4周）
- 编写单元测试（至少核心计算模块）
- 事件驱动回测集成到管线
- 样本外验证框架
- 幸存者偏差校正
- 数据质量验证
- 主动告警系统

### 阶段五：持续优化
- baostock 数据源接入
- 企业行动处理
- 策略对比框架
- 类型注解补全
- 剩余低优问题
