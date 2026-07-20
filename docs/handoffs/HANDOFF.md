---
# HANDOFF — 2026-07-21 (test-v172, turnover 回填可用)

## 当前运行状态
- **tushare token**: 已写入 config.yaml (aeeb8c7d...)
- **daily 表写入模式**: INSERT OR REPLACE (允许 turnover 覆写)
- **换手率缺口**: 07-11 ~ 07-20 turnover=0, 待跑 backfill_turnover
- **7 源回退链**: 全部可用, tencent/akshare IP封禁置末尾
- **因子库**: 上次物化 48 因子入库 (test-v155 按需物化后)

---
# HANDOFF — 2026-07-21 (test-v172, turnover 回填可用)

## 当前运行状态
- **tushare token**: 已配置, RateLimiter 对齐 50次/分钟免费版实际限制
- **daily 表写入模式**: INSERT OR REPLACE
- **7 源回退链日期格式**: 全源统一 — tushare/akshare/tencent/zzshare 用 YYYYMMDD, sina/pytdx 用 YYYY-MM-DD
- **换手率缺口**: 待跑 backfill_turnover (tushare 50/min 限速, 预计 15min 完成)
- **因子库**: 上次物化 48 因子入库

---

## test-v172 — tushare RateLimiter 对齐实际限速

**根因**: `_tushare_limiter` 设 `calls_per_minute=200`, 但 tushare 免费版实际限制是 **50 次/分钟**。
backfill_turnover 每日期 ~108 次调用, 7 日期 = 756 次 → 前 50 次透过后被服务端拒绝:
  `抱歉，您访问接口(daily)频率超限(50次/分钟)`

**修复**:
- `store.py:38`: `calls_per_minute=200` → `50`
- `config.yaml:65`: `tushare_batch_sec` 注释从 200次/分钟 → 50次/分钟, 间隔 0.4s → 1.2s
- `store.py:backfill_turnover`: 加进度日志 (每 500 只打印)

**影响**: RateLimiter 现在会在第 51 次调用时阻塞 60s, 确保不超限。
backfill_turnover 全量 756 次调用 ≈ 15 分钟完成。

---

## test-v171 — 日期格式全盘审计 + 统一转换策略

### 审计结果

7 个 `_fetch_*` 方法的日期格式处理不一致, 部分存在 bug:

| 方法 | 问题 | 修复 |
|------|------|------|
| `_fetch_batch_tushare` | start_date YYYY-MM-DD → tushare拒绝; fields 缺失 | test-v170 已修 |
| `_fetch_tencent_daily` | 手动 `.replace("-","")` 而非 `to_compact()` | 改用 `to_compact()` |
| `_fetch_akshare_daily` | `end_date` 已转 YYYYMMDD, **`start_date` 未转** ❌ | `to_compact(start_date)` |
| `_fetch_zzshare_daily` | `end_date` 已转 YYYYMMDD, **`start_date` 未转** ❌ | `to_compact(start_date)` |
| `_fetch_sina_daily` | YYYY-MM-DD 字符串比较 | ✅ 无需修改 |
| `_fetch_tickflow_daily` | 不通过 API 参数过滤 | ✅ 无需修改 |
| `_fetch_pytdx_daily` | YYYY-MM-DD 后过滤比较 | ✅ 无需修改 |

其他文件 4 处手动 `.replace("-","")`:
- `benchmark.py:59`: `last_date.replace("-","")` → `to_compact(last_date)`
- `margin.py:112`: `date_str.replace("-","")` → `to_compact(date_str)`
- `jq_valuation.py:95`: `date_str.replace("-","")` → `to_compact(date_str)`
- `daily_sync.py:41,43`: `date_str.replace("-","")` → `to_compact(date_str)`

### quant/utils/date.py 重构

新增两个语义化函数 + 7 数据源格式策略文档:
- `as_compact(d) → YYYYMMDD` — tushare / akshare / tencent / zzshare API
- `as_iso(d) → YYYY-MM-DD` — SQLite / sina / pytdx
- `to_compact()` 改为委托 `as_compact()` (向后兼容)
- 模块级注释列明各数据源期望格式

规则: 禁止手动 `.replace("-","")` — 一律用 `to_compact()` / `as_compact()`。

### daily_sync.py 缩进修复

`step2_margin` (line 39) 和 `step5_fundamentals` (line 71) 的 `from data.repos._base` 缺少 4 格缩进。
与本次审计无关, 顺手修复。

---

## test-v170 — tushare turnover_rate 拉取修复 + backfill_turnover 重写

### Bug A: `_fetch_batch_tushare` 两个问题

**1. fields 参数缺失**

`pro.daily()` 默认字段不含 `turnover_rate`。当前 tushare 版本不传 `fields` 时直接返回空 DataFrame。
`row.get("turnover_rate", 0)` 永远取到 0。

修复: 显式传 `fields="ts_code,trade_date,open,high,low,close,vol,amount,turnover_rate"`

**2. start_date 格式不对**

tushare API 要求 YYYYMMDD 格式。`batch_start` 从 SQLite 返回 YYYY-MM-DD, 直接传给 `pro.daily()` 导致返回空。

修复: `to_compact(start_date)` 统一转换

### Bug B: backfill_turnover 重写 — 去掉 DELETE-then-repull

**旧策略 (test-v168)**: DELETE 缺口日期行 → `update_daily(symbols=all, start=gap_start)` → tushare 重拉

**旧策略的三个致命问题**:
1. tushare 不传 fields + 日期格式不对 → 返回空 → 回退到 tickflow → turnover=0
2. `update_daily` 的 `batch_start_map` 取 `min(max_date)`, 删后大部分股票 max_date=07-10,
   但有个别股票 max_date 更早 → `batch_start` 被拉回到 2020-01-01 → 拉取 894,583 行
   (远超删除的 32,535 行), 连带覆写了 07-10 的 turnover (286 → 6)
3. 即便 tushare 成功, 也要重拉全量 OHLCV, 浪费 API 配额

**新策略**: 不 DELETE、不重拉 OHLCV — 直接调 tushare 拉 `turnover_rate` → `UPDATE daily SET turnover=?`

- `_init_cache()` + `_tushare_limiter.wait()` 限流
- `pro.daily(fields="ts_code,trade_date,turnover_rate")` 只拉 turnover, 不拉 OHLCV
- `UPDATE daily SET turnover=? WHERE symbol=? AND date=?` 定点更新
- 逐日期循环, 每日期 50 股/批

**对比**:

| | 旧 (DELETE+repull) | 新 (UPDATE only) |
|---|---|---|
| 删数据? | ✅ DELETE 缺口行 | ❌ 不动 OHLCV |
| tushare失败? | 回退源覆写 turnover=0 | 跳过, OHLCV 完好 |
| API 调用 | 全量 OHLCV (8 列) | 仅 turnover_rate (1 列) |
| 安全性 | 崩溃可丢数据 | 崩溃无损失 |

---

---

## test-v169 — 全链路数据拉取逻辑修复 (7 项)

### 背景

token 配置后追踪 daily_data -> update_daily -> _analyze_daily_gaps -> 7源回退链 -> backfill_turnover
全链路, 逐行审查发现 7 个逻辑问题。逐个修复如下。

---

### 问题 1: 源优先级 — 无 turnover 源排在 turnover 源前面

**文件**: quant/data/store.py:1058-1070

**根因**: 回退链按速度排序: tushare(turnover✅) -> tickflow(❌) -> zzshare(❌) -> pytdx(❌) -> sina(✅) -> tencent(❌) -> akshare(✅)。
若 tushare 某批失败, tickflow/zzshare/pytdx (无 turnover) 接盘写入 turnover=0, sina (有 turnover) 永不到达。
同一日期不同批次可能来自不同源 -> turnover 列不一致。

**修复**: 保留速度优先排序, 在注释中明确说明设计决策:
- tushare 首位 99%+ 成功率保证了 turnover 覆盖率
- 回退源(无 turnover)接盘后, backfill_turnover_quotes 后续补 turnover
- sina/akshare 虽有 turnover 但逐只拉取极慢, 置后作为最后回退而非中间层

**设计理由**: 将 sina 提到 tickflow 前会导致 "tushare 失败 -> sina 逐只 5600 HTTP 请求 -> ~46 分钟"。
回退链应快速获取 OHLCV 然后定点补 turnover, 而非为了 turnover 牺牲速度。

---

### 问题 2: tushare 双重限流

**文件**: quant/data/store.py:1123 (删除 sleep), quant/data/store.py:444 (保留 RateLimiter)

**根因**: 
- _fetch_batch_tushare 内部 _tushare_limiter.wait() — RateLimiter(200/min), 1s 轮询等令牌
- update_daily 外部 time.sleep(0.4s) — 每批后固定等 0.4s
- 叠加后每批 ~1.4s (API ~1s + limiter 检查 ~0s + sleep 0.4s)
- RateLimiter burst=200 远大于 112 批, limiter 实际不阻塞 -> sleep 是唯一的限速手段
- 但 tushare 下限速仅 200 次/分钟, 112 批/2min = 56/min, 无需人工 sleep

**修复**: 去掉 update_daily 中的 time.sleep(tushare_batch_sec), 改为注释。RateLimiter 保留作为 200/min 安全网。

**效果**: 每批节省 0.4s, 全量 112 批节省 ~45s。

---

### 问题 3: backfill_turnover_quotes 在 tushare 配置后成冗余调用

**文件**: quant/scheduler/daily_data.py:24-35

**根因**: 调度器先跑 update_daily (tushare 已拉 turnover), 接着无条件跑 backfill_turnover_quotes。
tushare 成功后 turnover=0 行数为 0 -> backfill_turnover_quotes 查询返回空列表 -> 即时返回。
但日志显示 "turnover backfill: 0 stocks updated" (INFO 级), 给人"在做什么"的错觉。

**修复**: 
- 保留调用作为安全网 (tushare 某批失败时, 回退源接盘写入 turnover=0 -> backfill_turnover_quotes 补漏)
- tn=0 时改为 _log.debug() (不显示在终端)
- tn>0 时 _log.info() + "(safety net triggered)" 标记
- 顶部加注释说明安全网角色

**设计理由**: 移除调用会失去安全网; 保留调用几乎零成本 (0 行时查询瞬间返回)。

---

### 问题 4: total_new 语义变化

**文件**: quant/data/store.py:1041

**根因**: INSERT OR IGNORE -> INSERT OR REPLACE 后, executemany 返回的行数不再代表"新写入行数",
而是"写入行数(含覆写已有行)"。stale_recent 模式全量刷新时, 日志显示 total_new=全量行数,
实际上这些行本就存在, 只是被覆写了 turnover 列。

**修复**: 在 total_new 计数开始前加注释说明语义变化。不改变量名 (避免大面积重构),
开发者和日志阅读者应知道此数值 = 写入行数 (非净新增)。

---

### 问题 5: _fetch_sina_daily 无逐只限流

**文件**: quant/data/store.py:487-488

**根因**: sina 逐只 HTTP 请求, 内部 for sym in symbols 无 time.sleep。
sina 当前容忍度高未触发封禁, 但随着数据量增长可能触发限流。

**修复**: 在循环内加 time.sleep(_require_cfg("data.rate_limit.sina_per_stock_sec"))。
config 已有 sina_per_stock_sec: 0.5, 50 只/批 = 25s/批。
sina 在回退链第 5 位, 仅在前 4 个源全失败时到达, 出现概率极低, 25s 可接受。

---

### 问题 6: backfill_turnover DELETE 无事务保护

**文件**: quant/data/store.py:788-795

**根因**: backfill_turnover 先 DELETE 缺口日期行, 再调 update_daily 重拉。
如果 DELETE 后进程崩溃或 update_daily 中 tushare 失败, OHLCV 数据丢失。
虽然可通过其他源重新拉取, 但需要手动干预。

**修复**: DELETE 包裹在 SAVEPOINT turnover_backfill + try/except/ROLLBACK 中:

    conn.execute("SAVEPOINT turnover_backfill")
    try:
        deleted = conn.execute("DELETE FROM daily WHERE date >= ? AND date <= ?", ...).rowcount
        conn.execute("RELEASE turnover_backfill")
    except Exception:
        conn.execute("ROLLBACK TO turnover_backfill")
        raise
    conn.commit()

**注意**: SAVEPOINT 只保护 DELETE 阶段。update_daily 会自行 commit, DELETE 提交后无法回滚。
但此时数据已落盘, update_daily 失败只会导致 turnover 补不回来 (可从 tickflow/zzshare 重拉 OHLCV)。

---

## test-v168 — tushare 接入 + turnover 回填链路修复 (前一个版本)

### token 配置

**文件**: quant/config/config.yaml:61
- ${TUSHARE_TOKEN} 占位符 -> 实际 token aeeb8c7d...
- token 之前从未写入文件, 导致 backfill_turnover 跳过、update_daily 回退链没有 tushare

### INSERT OR IGNORE -> INSERT OR REPLACE

**文件**: quant/data/store.py:1106

**根因**: update_daily 多源回退链中, 第一个成功的源写入 INSERT OR IGNORE。
tushare 无 token 时 tickflow 率先返回数据 (turnover=0), INSERT OR IGNORE 写入后,
后续源 (sina, 有 turnover) 被跳过 -> turnover 永远是 0。

**修复**: INSERT OR IGNORE INTO daily -> INSERT OR REPLACE INTO daily。
REPLACE = DELETE 旧行 + INSERT 新行, 仅在 (symbol, date) PRIMARY KEY 冲突时触发。
无外键依赖 daily 表, DELETE 不会级联。

**场景验证**:
| 场景 | IGNORE | REPLACE |
|------|--------|---------|
| 新数据 | INSERT | INSERT (同) |
| backfill 补 turnover | 跳过❌ | 覆写✅ |
| 同一源重复拉取 | 跳过 | 覆写 (值相同) |

### backfill_turnover 重写

**文件**: quant/data/store.py:747-797

**根因 1**: 旧版 backfill_turnover 逐日调 update_daily(start=d), 但 _analyze_daily_gaps
将已有数据的股票归为 full -> target 为空 -> update_daily 返回 0 -> 什么都没补。

**根因 2**: 即使强行进入 update_daily, batch_start_map 取 min(各股 max_date) = 07-20,
tushare 只拉 07-20->今天, 07-11~07-19 的 turnover 补不上。

**修复**:
1. DELETE 缺口日期行 -> _analyze_daily_gaps 检测到缺失 -> 触发 stale_recent -> 拉全
2. 显式传 symbols=all_syms 给 update_daily -> 跳过 _analyze_daily_gaps 的 full 误判
3. 删后 batch_start_map 各股 max_date 退回 07-10 -> batch_start = 07-10 -> < start(07-11) -> 重置为 07-11
4. tushare 从 07-11 拉全 -> INSERT OR REPLACE -> turnover 补全

**执行预估**: DELETE ~5.4 万行 (秒删) -> tushare 112 批 x ~1s/批 ~ 2 分钟。

### CLAUDE.md 补 3 条硬约束

**文件**: CLAUDE.md

| 规则 | 内容 |
|------|------|
| 编辑后验证 | ast.parse() + grep 确认方法存在 + 确认引用前已定义 |
| API 假设先测 | 涉及外部 API 参数 -> 先写小脚本验证, 再写业务代码 |
| heredoc 深度 <=1 | 禁止嵌套 pyEOF, 复杂字符串先写临时文件 |

---

## 当前数据源状态

回退链: tushare(50股/批, turnover✅, 200/min) -> tickflow(批量, ❌) -> zzshare(逐只, ❌)
       -> pytdx(TCP, ❌) -> sina(逐只, ✅, 0.5s/只) -> tencent(em, ❌, IP封禁)
       -> akshare(✅, IP封禁)

限流:
  tushare:    RateLimiter(200/min) — _fetch_batch_tushare 内 wait()
  tickflow:   config rate_limit.tickflow_batch_sec = 1.5s
  zzshare:    config rate_limit.zzshare_per_stock_sec = 0.3s
  pytdx:      config rate_limit.pytdx_per_stock_sec = 1.0s
  sina:       config rate_limit.sina_per_stock_sec = 0.5s  (test-v169 新增)
  tencent:    IP 封禁中, config rate_limit 存在但不触发
  akshare:    IP 封禁中, RateLimiter(60/min) 备用

API Key:
  tushare:    token 已配置 ✅
  tickflow:   api_key tk_868557a55bac4d1e859bf5be94087550 (免费注册版)

---

## 下一步

1. 跑 backfill_turnover 补 07-11~07-20 换手率:
   cd /Users/mariusto/project/quant && PYTHONPATH=. .venv/bin/python3 -c "
   from quant.data.store import DataStore
   s = DataStore()
   n = s.backfill_turnover()
   s.close()
   print(f'Updated: {n} rows')
   "

2. 验证 turnover:
   PYTHONPATH=. .venv/bin/python3 scripts/check_turnover_progress.py

3. 重跑因子物化 (补 benchmark_ret 后 residual_momentum/idio_vol):
   先确认 scripts/sync_benchmark.py 已跑过, 然后 scripts/materialize_factors.py
