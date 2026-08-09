# 全量代码审查报告 + 逐条修复方案

- 日期: 2026-08-09
- 范围: quant/ 全部 169 个 Python 文件（约 38,800 行），仅代码、不含文档
- 方法: 分层并行深读 + 对高危结论逐一实库/实码核验（✔ 标记已核验）
- 结论快览: 架构骨架健康，但存在 4 处 PIT 前视、系统性静默降级、评估统计口径错误、状态机与实盘记账通道未闭环、约 2500 行死代码。

---

## 目录

1. 审查总评（对应 7 个审查问题）
2. P0 修复清单（高危，建议 1-2 周内完成）
3. P1 修复清单（中危，建议 3-4 周）
4. P2 清理项（死代码/质量）
5. 修复顺序与回归验证策略

---

## 1. 审查总评

### 1.1 技术选型（问题 1）
- **合适**：Python + pandas/numpy/sklearn/LightGBM/XGBoost/SQLite/Flask 对单人研究型 A 股量化是"够用"组合；多源数据回退、gzip 因子缓存、manifest 声明式调度体现良好工程。
- **缺口**：
  1. 无 CI / pre-commit / mypy / ruff 门禁（`mypy --strict 零错误` 是规范但未接入）
  2. 无回测 run 管理（每次回测互相覆盖，见 P0-8）
  3. 无特征/数据版本化（`_rebase_ex_dividend` 改写全历史 OHLC）
  4. 实盘 Broker 记账断裂（VnpyAdapter 回调查空，见 P0-10）
  5. 并行评估 `evaluation/parallel.py` 必然失败（缺导入，见 P1-8）
  6. 无 CI/容器化/模型注册体系（mlflow 或等价物）
- **不建议换栈**；收尾工程化即可。

### 1.2 功能现状（问题 2）
已实现（代码实证）：14 类数据源、~90 因子 + 表达式策展器、sleeve/ic_weighted/LGB/XGB 合成、行业/市值中性化、Ledoit-Wolf、Kelly/HRP/Nano-Micro-Small 三级、ATR+移动止损+TP 阶梯+时间止损、涨停熔断 B8、T+1 校验、Walk-forward 回测、manifest 声明式调度 + 晚间链、8 阶段因子评估、Brinson 归因 + DSR、23 个 Web API。

缺口（按风险排序）：
1. 实盘 VnpyAdapter 记账闭环（最高风险）
2. 组合级风控约束未接线（constraints.py:212-247 `position_limit_check`/`sector_exposure_check` 无调用者 ✔）
3. 动态冲击模型/ TCA / 微观结构全部死代码，实盘只用 5bps 常数
4. 实盘止损 position_meta 跨日丢失，移动止损只能日内生效
5. 压力测试 Web 页恒显示 0 损失（P0-9）

### 1.3 业务闭环（问题 3）
- 主链闭环（数据→因子→Alpha→风控→优化→执行→监控→对账→归因→评估→重训）结构完整；回测 PIT 训练窗口核验无前视 ✔；manifest 依赖管理优秀。
- **四处断链**：
  1. monitor 崩溃 → reconcile 断 → daily_equity 不落库 → 净值/告警全断（P0-11）
  2. 回测命名永远 backtest_1 + 删历史交易（P0-8）
  3. 实盘记账断裂（P0-10）
  4. 因子状态机"永久淘汰/恢复"通道失效（P0-7）

### 1.4 架构（问题 4）
优点：7 层清晰、manifest 单一真相源、TradeRepo 迁移正确性加固。
需要重构：
1. **双路径计算**：物化走 `FACTOR_SHORTCUT`、golden_test 走原始函数，6 个因子两条路径行为不等价 → 回归保护只覆盖原始路径（P2-1）
2. alpha 层反向依赖 factor 层（synth→intersection）
3. 回测写生产表 `factor_ic_daily(scope='backtest')`
4. web/app.py 1034 行含业务 SQL，Controller 无分层
5. 重复实现 5 处（换手/压力/ATR/cost/街道）
6. 死代码 ~2500 行

### 1.5 代码质量（问题 5）
裸 except 30+ 处（pipeline.py:393 except:pass ✔）；静默降级蔓延（alpha/model.py:139-148 ML 失败回落 ic_weighted ✔）；文档与实现漂移（ATR ↑ EMA 实际 SMA、印花税注明千一实际 0.0005、quote 量纲手/股互斥）；`from data.xxx` 违反导入规范（daily_sync.py）；N+1 查询（trade_repo.get_positions）。

### 1.6 算法（问题 6）— 需讨论后再改
PIT 前视 4 处（P0-1/4/7 相关）、ATR 回测前视（P0-4）、中性化 NaN 传染（P0-3）、DSR 量纲（P0-6）、half-life 公式（P1-11）、PBO 幸存者偏差与阈值脱节（P1-12）、Ledoit-Wolf 简化实现、HRP 对角线近似、Kelly 每股方差常数化、`calibrate_risk_aversion` 当日截面选参、HMM 状态漂移假设。

### 1.7 结论
单位/日期/口径三类 bug 是最大面：universe 年份错位、三源 market_cap 单位、CPI 日期粒度、margin 日期格式、DSR 年化/每期口径。修复次序：P0 → P1 → P2。

---

## 2. P0 修复清单（必须先修）

### P0-1 [高危] universe 日期格式错位（年份级漏票）✔
- 位置: `quant/data/store.py:370-381` `get_universe`; `quant/data/repos/universe_repo.py:119-127`
- 根因: `stocks.list_date` 存 `YYYYMMDD`（8 位, 实库确认），查询参数为 `YYYY-MM-DD`（ISO）。字典序比较在 '0'(0x30) vs '-'(0x2D) 处错位 → **当年上市的股票整年被排除在范围外**。
- 修复（最小, 查询侧统一）:
  ```python
  # store.py get_universe
  query = (
      "SELECT symbol FROM stocks "
      "WHERE list_date <= strftime('%Y%m%d', ?) "
      "  AND (delist_date IS NULL OR delist_date > strftime('%Y%m%d', ?)) "
      "  AND market != 'BJ'"
  )
  # universe_repo.py 同步: end_date → list_date <= strftime('%Y%m%d', ?)
  #                    start_date → delist_date > strftime('%Y%m%d', ?)
  ```
- 注意: 若 delist_date 存的是 ISO，`strftime('%Y%m%d', ISO)` 也正确；同一列两种格式时需先核查。建议写 SQLite 迁移统一 list_date/delist_date 全部为 ISO（P1）。
- 验证: 单测插入 list_date='20240105' 的股票，`get_universe('2024-12-31')` 必须包含；对照 2024 上市 77 只回归。
- 影响: 所有历史回测 stock pool。

### P0-2 [高危] market_cap 三源三单位，total_mv 失真 ✔
- 位置: `store.py:2392-2394`（读侧无条件 ×1e8）；`data/jq_valuation.py:64-72`（tushare 回退写万元）
- 根因: daily_valuation.market_cap 列: eastmoney 源写入"元"（占比 95%，实库确认），jqdata 源写"亿元"，tushare 回退写"万元"。读侧统一 ×1e8 只对 jqdata 源正确。
- 修复（写入侧统一为"元" + 读侧按源修正存量）:
  ```python
  # 读侧(store.py) — 兼容存量数据
  if "market_cap" in val_df.columns:
      mc = val_df["market_cap"]
      src = val_df.get("source", None)
      if src is not None:
          conv = pd.Series(1.0, index=mc.index)
          conv[src == "jqdata"] = 1e8      # 亿元→元
          conv[src == "tushare"] = 1e4     # 万元→元
          df["total_mv"] = mc * conv
      else:
          df["total_mv"] = mc * 1e8  # 旧行为（单独处理）
  # 写侧: jq_valuation.py TUSHARE_TO_JQ_MAP total_mv → market_cap 时 ×1e4 转元
  #       em_valuation 确认按元写入（现为元, 不动）
  ```
- 验证: 600519 total_mv ≈ 1.6e12；跨 2025-10 源切换无断点；单测 mock 三种源。
- 影响: 市值因子、市值中性化、规模因子。

### P0-3 [高危] 批量中性化 NaN 传染 → 整日因子消失 ✔
- 位置: `quant/risk/neutralize.py:308-315` `_apply_neutralize_batch`（pipeline.py:411 每日调用）
- 根因: `aligned` 含 NaN（停牌/新股无因子值）→ 稠密投影 `P @ y` 的行全部 NaN → 该因子全体被静默丢弃（标量版 `_joint_neutralize` dropna 后回归则无此问题）。
- 修复（dropna 切片投影，与标量路径语义一致）:
  ```python
  valid = aligned.dropna()
  if len(valid) < 2:
      return scores  # 或保留原值, 由上层 dropna 裁决
  y = valid.values.astype(np.float64)
  Pv = P.loc[valid.index, valid.index]   # P 是 DataFrame 时; 若 P 是 ndarray, 用 idx 数组切片
  residuals = Pv @ y
  result = pd.Series(residuals, index=valid.index)
  result = (result - result.mean()) / result.std(ddof=1)
  return result.reindex(scores.index)
  ```
- 验证: 构造 99 只有效 + 1 只 NaN 的截面，断言 99 只残差非 NaN；对比标量路径输出一致（≤1e-8）。

### P0-4 [高危] 回测 ATR 使用未来行情（系统性前视）✔
- 位置: `quant/execution/stop_loss.py:30-62` `_compute_atr`; `check_hard_stop`:90; `check`:175
- 根因: `_compute_atr` 无日期参数，`ORDER BY date DESC LIMIT period+1` 取的是 market.db **最新**数据；回测历史日期=用未来行情；且 120s TTL 缓存跨多日复用。
- 修复:
  1. `_compute_atr(symbol, period, as_of: str)` → SQL `WHERE date <= ? ORDER BY date DESC LIMIT ?`; 缓存 key 加 `as_of`。
  2. `RiskManager.check_hard_stop(positions, prices, today)` 与 `check(positions, quotes, today)` 已收 today ✔ → 全线下传至 `_compute_atr`；调用方 `execution_model.py` 已传 ctx.today。
  3. 回测中若 `< period` 行则短路返回 0（现逻辑），加日志。
- 验证: 单测: 回测 2024-01-02，market.db 有 2026 数据，断言 ATR 与 2026 数据无关。
- 影响: 全部回测的止损/止盈/移动止损/时间止损阈值。

### P0-5 [高危] XGB 预测特征缺列错位（静默垃圾输出）✔
- 位置: `quant/alpha/xgb_model.py:237-244` `predict`（同模式在 qlib_model.py:556-560 `EnsembleAlphaModel.predict` 与 `shap_explain`）
- 根因: 列表推导用 `if fn in factor_values` 过滤缺失特征，缺失的列在末尾补 0 → 中段缺失时后续特征**列位移** → 预测值静默全错（qlib_model.py:273-279 有 v406 注释已修同款，xgb 端未移植）。
- 修复（按特征序位置零，不压缩列序）:
  ```python
  X = np.column_stack([
      factor_values.get(fn, pd.Series(0.0, index=symbols))
      .reindex(symbols).fillna(0.0).values
      for fn in self._feature_names   # 去掉 `if fn in factor_values` 过滤
  ])
  # 移除 pad 逻辑; 保留缺失特征 warning 日志
  ```
  同步修正 `EnsembleAlphaModel.predict` 与 `shap_explain`。
- 验证: 训练 3 特征模型，断言缺中间特征时 feature_1 列不受污染; NU单测对比手动列构造。

### P0-6 [高危] DSR/PSR 量纲错误（显著性恒 1.0）✔
- 位置: `quant/evaluation/deflated_sharpe.py`（PSR/MinTRL）; 调用方 `loop.py:144-166`、`attribution.py:430-447`（均传年化 SR + 日频 n_obs）；`cpcv_dsr.py:101`（传日频 ICIR，正确）
- 根因: PSR 公式方差项 `(1 - γ3·SR + (γ4-1)/4·SR²)/n` 要求 SR 与 n 同周期；年化 SR 与日频 n 混用 → 方差被放大 ~244 倍 → 正收益策略 DSR≈1.0，MinTRL 荒谬（≈0.03 年）。
- 修复（最小改动、与 cpcv_dsr 口径统一: 每期口径）:
  ```python
  # deflated_sharpe.py 保持公式不变 (每期口径, 与 cpcv_dsr 调用一致)
  # loop.py _compute_dsr:
  sr_daily = float(np.mean(excess) / np.std(excess))     # 不加 sqrt(ann_days)
  # attribution.py compute_dsr_for_strategy 同理:
  #   observed_sr 传入不带年化的每期 SR (mean/std), 其余参数不变
  #   annualized_sr 仅用于显示/日志, 不参与 PSR 计算
  ```
- 验证: 构造年化 SR=0.8、500 天、T=8 试验 → 修正后 DSR∈(0.5,0.9)（修正前恒 1.0）；数值对照 mlfinlab 实现。
- 影响: G3 归因、回测 MDD 报告、因子评估 DSR 门。

### P0-7 [高危] 因子状态机"永久淘汰"与"恢复"通道失效 ✔
- 位置: `quant/evaluation/phase5_monitor.py:157-166,202-214`; `quant/factor/state_manager.py:54` `_VALID_EVENTS`
- 根因:
  1. `EVAL_REJECT` 不在 `_VALID_EVENTS`（= {EVAL_PASS, EVAL_MARGINAL, EVAL_FAIL, IC_DEGRADED, IC_RECOVERED, IC_PERSISTENT, FACTOR_REDUNDANT, DATA_SOURCE_DEAD, RETRY_RESTORE}）→ `transition()` 抛异常被 `except: warning` 吞 → "永久淘汰"永不落地，因子停在 evaluating 无限重试。
  2. retry 双重递增（47 行 first 层 +1，203 行再 +1→DB 写 old+2），max_retries=3 时失败 2 次即"永久淘汰"。
  3. probation 因子调 `EVAL_PASS`（仅 evaluating→active 合法）→ 异常被批处理吞 → 滞留 probation 永不恢复。
- 修复:
  ```python
  # 事件选择 (phase5)
  if current_status == "evaluating":
      event = "EVAL_FAIL"                    # 非法事件 → 归档
  elif current_status == "probation":
      event = "IC_PERSISTENT"                # 无法恢复 → 归档
  else:
      continue                                # active 不在此处裁决
  # probation 恢复用 IC_RECOVERED:
  for name in recovering_to_active:
      fsm.transition(name, "IC_RECOVERED", ...)   # 而非 EVAL_PASS
  # retry 计数: 只在一个地方 +1 (transition 前), 删除第一层预增
  ```
- 验证: state_manager 单测 3 事件合法转换 + retry=3 次失败后 status==archived; phase5 集成测试。

### P0-8 [高危] 回测命名覆盖（不可对比）+ 清理 ✔
- 位置: `backtest/loop.py:336-342` DELETE + `backtest/naming.py:25` 查询 DB
- 根因: naming.next_name 查询**实盘 TRADE_DB**（strategy_config），回测策略在 `BACKTEST_DB=data/backtest_trades.db`（loop:59）→ 永远返回 backtest_1 → 每次 run_backtest 把旧交易 DELETE 清空。
- 修复（最小）: `naming.next_name(prefix, db_path=...)` 增加参数，loop.py 传 `BACKTEST_DB`；同时把 DELETE 改为"仅清理同名策略订单、保留净值曲线"或新增 `run_id` 列。推荐: 回测事务 + run_id（data/model 需 migrations）。
- 验证: 连续两次 run_backtest 名字递增且不等。

### P0-9 [高危] 压力测试 Web 恒 0 损失 ✔
- 位置: `quant/risk/var.py:163`
- 根因: `position_val = weights.get(sym, 0) if isinstance(weights, dict) else 0` — risk_report 传的是 pd.Series → 恒 0。
- 修复:
  ```python
  if isinstance(weights, dict):
      position_val = weights.get(sym, 0)
  elif isinstance(weights, pd.Series):
      position_val = float(weights.get(sym, 0))
  else:
      position_val = 0
  ```
- 验证: Web /api/risk 压力字段非零；单测 Series 权重。

### P0-10 [高危] 实盘 Broker 记账断裂 ✔
- 位置: `quant/execution/broker_adapter.py:334-390` `VnpyAdapter._on_trade`/`_on_order` 为空
- 根因: 实盘成交回调不写入 sim_trades / 更新 OMS 状态 → 持仓、现金、T+1、止损判定全部基于陈旧 SQLite。
- 修复（分阶段）:
  1. 立即（安全阀）: `VnpyAdapter.connect` 中检测未实现 → `raise NotImplementedError` 或前端禁选，禁止带假记账上线。
  2. 中期: `_on_trade` 内 `TradeRepo().record_trade(...)`（与回测 broker 同架构）+ `_on_order` 更新 pending_orders 状态; 新增每日盘前/盘后市场对账（可复用 reconcile 的对比逻辑）。
  3. 配置: 增加 `execution.broker.adapter` 白名单校验。
- 验证: 模拟器测试（vnpy gateway 测试环境）→ 成交后 get_positions 一致。

### P0-11 [高] monitor 崩溃 → 当日永不恢复 + reconcile 断链 ✔
- 位置: `quant/scheduler/orchestrator.py:92-99, 244-260`; `manifest.py:105`
- 根因: `_should_run`: `cur == "failed" → False`（永不重试）；monitor 死进程 pid 存活 + timeout_s=None → 兜底也失效；reconcile 依赖 monitor==ok → nightly 净值/日报断链。
- 修复:
  1. 允许当日重试: `cur == "failed"` 时若 `aborted < _MAX_TASK_RETRIES` 且仍在窗口内 → 返回 True（重试次数计入当日 failed 计数）。
  2. `_get_monitor_failures` 之外补充: monitor 线程崩溃时 status 写 failed 而非 running。
  3. reconcile 依赖改为 `depends_attempt=("monitor",)` 或 manifest 增加 fallback 依赖，保证 monitor 失败也执行对账（对账本身不依赖 monitor 结果）。
- 验证: 注入 _monitor_daemon 异常 → 下一周期重启动; reconcile 正常运行。

---

## 3. P1 修复清单

### P1-1 [中] 节假日表 2025-04-07 错标 ✔
- `quant/execution/calendar.py:32` 删除 "2025-04-07"（当天为交易日，清明节 4/4-4/6）。
- 补充 2020-2024 节假日（从 `data/trade_calendar.json` 或交易所公告核对）; 本地兜底逻辑增加 `trade_calendar.json` 查询。
- 验证: `is_trading_day("2025-04-07")==True`。

### P1-2 [中] quote.py volume 单位混（手 vs 股）✔
- `quant/execution/quote.py:106` 腾讯 `fields[6]` 为"手" → `×100` 与新浪一致；版本注释。成交量一致性校验（连续 3 日 vs daily 表中 amount/vol 对比）。

### P1-3 [中] NoopBackend.get TTL 恒 +1h ✔
- `quant/data/cache.py:46-56`: `set` 存过期时刻，`get` 判断因此错误。修复: `if time.time() < ts: return data`（删除 3600 硬编码）。

### P1-4 [中] 令牌桶限流器 <60/min 时"死锁"+ 调用方忽略返回值 ✔
- `quant/data/cache.py:76-99`: 拒绝时 `last=now` → 每次 1s 轮询 elapsed≈1 → int() 截断 0 → 永不 refill。调用方（store.py:566 等）无视 `wait()` 返回值，超时照打 AP.
- 修复:
  ```python
  tps = max_calls / window_sec          # 支持小数
  tokens = min(cap, tokens + (now - last) * tps)
  if tokens >= 1:
      self._buckets[ns] = (tokens - 1, now) if tokens - 1 > 0 else (cap, now)
      ...
  self._buckets[ns] = (tokens, now)      # last 更新, 不重置
  ```
- 调用方 `wait()` 返回 False → fail-fast（raise RateLimitExceeded 或跳过该源并告警），禁止静默继续。

### P1-5 [中] news_sentiment (symbol,date) 唯一 → 新闻互相覆盖 ✔
- `quant/data/news.py:24-34,124-127`: 改 schema: 主键 `(symbol, date, content_hash)` + `INSERT OR IGNORE`（保留全部新闻）; `news_daily_count` 按当天行数 COUNT 或改语义为 COUNT(DISTINCT stock)。需要 migrations SQL（docs/migrations/NNN）。
- 验证: 同日两条新闻均入库; 情绪因子均值与手工聚合一致。

### P1-6 [中] 财务 PIT: stat_date+90 前视 / 无上界 / 股本非 PIT ✔
- 位置: `factor/compute/fundamental.py` `_get_financial_historical`(:183-200)、`compute_asset_growth`(:志690+), `compute_ocfp`(:1022), `compute_sue`(:778-795 当前股本)
- 修复（分步）:
  1. 立即: `_get_financial_historical` `params=(date,)` 去掉 +90d 容忍；并每 symbol 只取 `stat_date ≤ date` 中最新一行（PIT 语义）。`compute_asset_growth`/`compute_ocfp` 补 `stat_date <= ?` 上界。
  2. 中期: 引入公告日（announce_date）字段（JQ 数据源提供），SQL `WHERE announce_date <= date`。
  3. `compute_sue` 用股本时间序列（`stocks_history.share` 或日股本表）替换当前 total_shares。
- 验证: 单测: date=2024-03-15 时 2023 年报（公告 2024-04-30晚于）不进入; 各因子 IC 与人工 PIT 计算一致。
- 影响: 所有基本面因子回测信号。

### P1-7 [中] factor_cache._run 越过请求 end_date + CLAUDE.md 命令签名失配 ✔
- `quant/scheduler/factor_cache.py:39-44`: 手动 `_run('2019-01-01','2019-12-31')` 会把 2020-2026 误物化（`actual_end = max(end_date, _latest)`）。
- 修复: `actual_end = min(end_date, _latest)`（当 extrema 请求时以请求为准 + warning）; 同步更新 CLAUDE.md 命令为双参数 `_run('2020-01-01','2020-01-01')`。
- 验证: 上述命令后缓存目录只出现 2019 年末日期文件。

### P1-8 [中] evaluation/parallel.py 必崩（缺 import）✔
- 模块顶部加 `import pandas as pd` 与 `from quant.config.constants import _require_cfg`（当前 pandas import 在函数内不可达 worker，'from quant.config...' 未导入）。

### P1-9 [中] half_life 公式缺 ln2 ✔
- `quant/evaluation/phase2_single.py:99-107`: `int(-20/np.log(ratio))` → `int(19 * np.log(2) / np.log(ratio))`（ratio = IC20/IC1 ≈ e^{19λ} — 两观测点间隔 19 天）。ratio=0.5 时 28.9d→19.0d，min_half_life=20 门禁变化。
- 验证: 单测公式 3 组数据。

### P1-10 [中] PBO 门禁阈值脱节 + 幸存者偏差 ✔
- `quant/evaluation/pbo.py:72-76` 硬编码 0.3; `phase3_oos.py:165-171` 报错也按 0.3; config.yaml `pbo_max: 0.2` 不生效。
- 修复: `logit_threshold = np.log(pbo_max/(1-pbo_max))` 从 config 读取，两处统一; phase3 报错消息同步。
- 附带: PBO 矩阵应含 Phase2 全候选（含被淘汰因子）以免低估过拟合（需 phase2 输出结构改动，行 P2 处理）。

### P1-11 [中] `_iterative_clip` 全超限时违反 max_single ✔
- `quant/optimizer/portfolio.py:186-191`: `over.all()` 时等权 1/n，n 小（如 3 只）时单票 33% > max_single 5%。
- 修复: 全超限时 `w = np.full(n, max_single); w /= w.sum()`（clip 到上限再归一）+ warning 日志（约束不可满足时显式暴露，而不是静默违反）; 若归一后仍 > max_single 则再次 clip 循环收敛（最多 max_iter）。

### P1-12 [中] margin / daily_sync 日期格式混写 ✔
- `quant/data/margin.py:83` 与 `daily_sync.py:step2_margin` 写入 YYYYMMDD，与库内 YYYY-MM-DD 混写同列。
- 修复: 写侧统一 `%Y-%m-%d`; 加断言校验（INSERT 前检查); 补历史脏数据修数据脚本（2024 年 K 行）。

### P1-13 [中] industry_neutralize 单股票行业 → NaN 删股 ✔
- `quant/risk/neutralize.py:51-58`: 单只股票行业被 z-scored（std=NaN）→ 股票被删。修复: 该行业 sample < 3 时跳过中性化（保留原值），与多股分支一致。

### P1-14 [中] pipeline.L393 包 except:pass ✔
- 替换为显式 exception 记录 + 列表为空时 fail-fast（抛异常终止当日信号生成）或告警降级但留 trace。

### P1-15 [中] 卖出缺报价 → 成本价成交（PnL 恒 0 / 误成交）✔
- `execution_model.py:186-189` / `pipeline.py:673-675` / `scheduler/execute.py:102-104`: 缺报价时**阻断卖单**并告警（宁可缺仓不可错价）。

### P1-16 [中] 除权检测与数据口径
- `engine.py:92-125` `_check_ex_dividend`: 逻辑正确（date< ? 前收盘，✔ 无前视），但依赖实时 market.db 逐笔查询（性能）; 送转除权日 gap 超限被跳过买入是"设计"但 qfq 数据下本不应出现 → 建议改用 adj_factor 表判定真实除权日，减少误判; 复牌跳空（真实交易价）不应阻挡买入。
- 回测每次买入发起 DB 查询 → 预计算当日全部 ex-div 股票集合。

### P1-17 [中] factor_curator 重复注册/静默吞异常 ✔
- `factor_curator.py`: `turnover_accel` 两处注册 → 去重; `except Exception: warning` → 记录因子名+表达式+撤销注册标记; 表达式含金融表字段走价格路径 → 字段缺失检测（compile 时校验 _FIELD_MAP + 表权限）。

### P1-18 [中] stocks.total_shares 列无建表/迁移 ✔
- 仓库 grep 无 ALTER;（实库已手工加过，全新部署会崩溃）。补 `store.ensure_tables` 幂等 ALTER + `docs/migrations/NNN-*.sql`。

### P1-19 [低] trade_repo get_positions N+1 ✔
- 一次 `SELECT ... GROUP BY symbol` 聚合代替每持仓全表读入。

### P1-20 [低] report.py 净值无 strategy 过滤
- `monitor/report.py:96-105` `SELECT date, total_equity` 不带策略 → 多策略交织。修复: 绑定当前默认策略（同 alerts.py 一致问题: alerts.py:31 用默认 "quant" — 用 config `strategy.name` 统一）。

---

## 4. P2 批量

1. **[模] 双路径统一**: 物化改为统一走原始函数（或 shortcut 与原始之间生成一致性断言 + golden 双路互验, `golden_test.py` 改为生产路径参数传入）。
2. **[架构] 回测写生产表**: loop/oos_verify 回测 IC 改写独立 `factor_ic_daily(scope='backtest')` → 独立表或独立 DB（backtest_trades.db 内）。
3. **[架构] alpha 层去反向依赖**: 将 intersection 相关函数下沉到 alpha/ 或公共模块, factor 不再导出。
4. **[dead] 删除清单**: `risk/atr.py`、`risk/stress_test.py`（被 var 内联替代）、`execution/tca.py`、`execution/market_microstructure.py`、`backtest/bridge.py`、`evaluation/parallel.py`（修复后保留）、`evaluation/factor_diagnostics.py`（空实现）、`alpha/EnsembleAlphaModel/rolling_train_cv`（无调用，或先修标准化再启用）、`compute/price.py.bak`、各模块无调用函数 13+ 个（vol_ratio/turnover_change/downside volatility/hsgt_flow/turnover_vol/mif/idio_turnover_vol/turnover_accel_5_20/vp_divergence/bb_pct_b/bb_width/bb_squeeze/main_flow_ratio）、`_preload.py` seconds 清理 fund_flow/pledge 残留，`ic.py:192-194` dead 块。
5. **[架构] web 分层**: /api/positions、/api/risk 等路由内业务 SQL 抽到服务层。
6. **[工程]**: pre-commit (ruff+mypy+pytest)、G itHub Actions 主链路、VERSION 版主流程保持。

---

## 5. 修复顺序与回归验证策略

| 阶段 | 内容 | 回归手段 |
|---|---|---|
| A (本周) | P0-1…P0-11 | 每个修复配单测（≥3 用例: 正常/边界/异常）+ golden_test 双路径; 跑 2024-01-01~2025-12-31 精选回测对照修复前后指标差异，**预期: 部分正收益"缩水"（前视被清除）** |
| B (2周) | P1-1..P1-12 | 小规模 n_symbols≤100 冒烟 + 增量单测; 修复 P1-6 后重跑 Phase2/3 因子库确认状态迁移 |
| C (3-4周) | P2-1..P2-6 | 死代码删除后全量回归（test/ 全量 pytest）；CI 接入 |

**回归铁律**: 每改一档前把前档所有测试全绿；P0 修复合入前 HANDOFF 更新 + VERSION bump（按 repo 规范用 re.sub）。

---

## 附: 已核验证据索引（复核时用）

| Bug | 证据 |
|---|---|
| P0-1 | market.db: SELECT list_date FROM stocks LIMIT 5 → '19910403'; get_universe 查询参数为 '2024-12-31' |
| P0-2 | market.db: daily_valuation 600519 最新行 market_cap=1.6366e12 (元) source=eastmoney; jq data 行为 亿 |
| P0-3 | neutralize.py:309 `aligned.values.astype(np.float64)` + P 稠密投影 |
| P0-4 | stop_loss.py:33-45 SQL 无 date 上界 |
| P0-5 | xgb_model.py:237-244 列表过滤 if fn in factor_values + 末尾 pad |
| P0-6 | deflated_sharpe.py:41-48 + loop.py:154 (年化) + attribution.py:443 |
| P0-7 | state_manager._VALID_EVENTS (含 EVAL_REJECT) + phase5:216 |
| P0-8 | loop.py:59 BACKTEST_DB vs naming.py:25 TRADE_DB |
| P0-9 | var.py:163 Series 权重 |
| P0-10 | broker_adapter.py _on_trade/_on_order 空实现 |
| P0-11 | orchestrator.py:92-99 `failed→False`，monitor 线程 pid 兼容失败 |
| P1-3 | cache.py:48-55 set/exp 混淆 |
| P1-4 | cache.py:76-91 int() 截断 + last 重置 |
| P1-6 | fundamental.py:183-200 stat_date+90d |
| P1-8 | parallel.py 顶层无 pandas/_require_cfg |
| P1-13 | neutralize.py:51-58 单股票行业 z=NaN |
| P1-17 | factor_curator 重复注册/吞异常 |

---
*本报告由代码审查生成（2026-08-09），仅基于代码与实库验证，不代表已完成修复。*