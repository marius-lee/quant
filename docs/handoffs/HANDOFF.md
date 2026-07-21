---
# HANDOFF — 2026-07-21 (test-v173, turnover 数据链防御体系)

## 当前运行状态
- **tushare token**: 已配置
- **daily 写入模式**: INSERT ... ON CONFLICT DO UPDATE (turnover 受 CASE WHEN 保护)
- **_fetch_batch_tushare**: fields 不含 turnover_rate (API 不支持该字段)
- **backfill_turnover**: sina 源 + socket 超时防护
- **7 源回退链**: 全部可用, tencent/akshare IP封禁置末尾
- **因子库**: 上次物化 48 因子入库

---

## test-v173 — turnover 数据链三重防御 (2026-07-21)

### 根因链

信号生成崩溃 direct cause: `pipeline.py:134` KeyError 'close' — get_symbols() 返回 0 只股票。

完整因果链 (5 层):
1. `_fetch_batch_tushare` 传 `turnover_rate` 字段 → tushare daily API 不支持 → 返回空
2. Fallback 源 (tickflow/zzshare/pytdx) 无 turnover 字段 → INSERT OR REPLACE 覆写 turnover=0
3. test-v168 DELETE-then-repull → batch_start 拉到 2020 → 89 万行被覆写
4. UniverseRepo get_symbols() `_ref_turnover`='2026-07-10' (仅 6 只 BJ 股有 turnover>0)
5. BJ 市场被 exclude_market 排除 → 0 只股票 → 空 DataFrame → KeyError

数据验证:
- turnover 覆盖率历史: 2025-11=17.9% (965只) → 2026-01=0.6% (35只全BJ) → 07-10=0.1% (6只BJ)
- akshare IP 封禁后 turnover 覆盖断崖式下跌; tushare 因字段错误从未成功提供 turnover
- 07-11~07-20 所有股票 turnover=0

### 修改 1: _fetch_batch_tushare — 移除 turnover_rate 字段

**文件**: quant/data/store.py:463

**改前**: `fields="ts_code,trade_date,open,high,low,close,vol,amount,turnover_rate"`
**改后**: `fields="ts_code,trade_date,open,high,low,close,vol,amount"`

**原因**: tushare `daily` API 不含 `turnover_rate` 字段 — 该字段仅在 `daily_basic` API 存在 (免费版 1 次/分钟, 无法批量用)。传不存在字段导致 API 返回空 DataFrame → fallback 源接盘 → turnover=0。

**来源**: 2026-07-21 `scripts/test_tushare_turnover.py` 实测 — `pro.daily()` 返回列不含 `turnover_rate`。

连带: `_norm_row` 的 turnover 参数从 `row.get("turnover_rate", 0)` 改为 `float(0.0)`, 注释说明 tushare 不提供 turnover, 由 backfill 补充。

### 修改 2: INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE

**文件**: quant/data/store.py:1149

**改前**:
```sql
INSERT OR REPLACE INTO daily
(symbol,date,open,high,low,close,volume,amount,turnover)
VALUES (?,?,?,?,?,?,?,?,?)
```

**改后**:
```sql
INSERT INTO daily
(symbol,date,open,high,low,close,volume,amount,turnover)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(symbol, date) DO UPDATE SET
open=excluded.open, high=excluded.high, low=excluded.low,
close=excluded.close, volume=excluded.volume, amount=excluded.amount,
turnover=CASE WHEN excluded.turnover > 0 THEN excluded.turnover ELSE turnover END
```

**原因**: INSERT OR REPLACE = DELETE + INSERT, 整行替换无法选择性保留列。fallback 源 (无 turnover) 写入时覆写 turnover=0, 破坏已有数据。ON CONFLICT DO UPDATE 允许 `CASE WHEN` 逐列判断: 新源 turnover=0 时保留旧值, turnover>0 时更新。

**来源**: SQLite 3.24+ UPSERT 语法; daily 表已有 PRIMARY KEY (symbol, date)。

### 修改 3: backfill_turnover — sina socket 超时防护

**文件**: quant/data/store.py:808

**改前**: `urllib.request.urlopen(req, timeout=_require_cfg("data.http_timeout.tushare"))`
**改后**: `_socket.setdefaulttimeout(_require_cfg("data.http_timeout.sina"))` 在每次 urlopen 前设置 socket 级超时

**原因**: `urllib.request.urlopen` 的 timeout 仅覆盖连接阶段, 不覆盖 HTTP response read 阶段。sina 某些请求在 read 阶段永久挂起 (实测), 导致 backfill 卡死。`socket.setdefaulttimeout` 设置 socket 级超时覆盖 read 阶段。

**来源**: 2026-07-21 backfill_turnover hang 实测 (KeyboardInterrupt at `http.client` read 阶段)。

**配置**: `config.yaml` 已有 `http_timeout.sina: 10` (秒)。

---

## 当前数据源状态

回退链: tushare(50股/批, ❌无turnover, 50/min) -> tickflow(批量, ❌) -> zzshare(逐只, ❌)
       -> pytdx(TCP, ❌) -> sina(逐只, ✅turnover, 0.5s/只) -> tencent(em, ❌, IP封禁)
       -> akshare(✅, IP封禁)

限流:
  tushare:    RateLimiter(50/min) — _fetch_batch_tushare 内 _tushare_limiter.wait()
  tickflow:   config rate_limit.tickflow_batch_sec = 1.5s
  zzshare:    config rate_limit.zzshare_per_stock_sec = 0.3s
  pytdx:      config rate_limit.pytdx_per_stock_sec = 1.0s
  sina:       config rate_limit.sina_per_stock_sec = 0.5s + socket timeout 10s
  tencent:    IP 封禁中
  akshare:    IP 封禁中

API Key:
  tushare:    token 已配置 ✅
  tickflow:   api_key tk_868557a55bac4d1e859bf5be94087550

---

## 下一步

1. 重启 web 服务 (用户执行)
2. 跑 sina backfill_turnover 补 07-11~07-20 换手率:
   ```bash
   cd /Users/mariusto/project/quant && PYTHONPATH=. .venv/bin/python3 -c "
   from quant.data.store import DataStore
   s = DataStore()
   n = s.backfill_turnover()
   s.close()
   print(f'Updated: {n} rows')
   "
   ```
3. 验证 turnover:
   ```bash
   PYTHONPATH=. .venv/bin/python3 scripts/check_turnover_progress.py
   ```
4. 手动跑信号生成验证不再崩溃
