# 独立代码审计与路线图 (2026-07-26)

> 来源: 不依赖既有文档叙述, 直接阅读代码取证 (150 py 文件 / ~15k LOC),
> 对标 qlib / LEAN / zipline / Alphalens / WorldQuant 研究流程。
> 执行状态见文末核对表。审计人: Codex (应用户要求, 旧文档存疑背景下重做)。

## 一、已达业界水准的部分 (代码实证)

- **因子研究卫生**: registry 状态机 (candidate→monitoring→active→retired)
  + CPCV purged walk-forward (`quant/evaluation/cpcv.py`) + DSR 多重检验校正
  (`deflated_sharpe.py`, Bailey & Lopez de Prado 2014) + PBO — De Prado 全套,
  小团队中罕见。
- **执行真实度**: Almgren-Chriss 冲击模型 (`execution/impact.py`)、T+1、
  涨跌停检测 (分板块, `_get_board_limit`)、整手、止损、周频调仓 + 风控日、
  限价单被动成交 (ADR 033)。
- **运维**: 单线程编排器 (`scheduler/orchestrator.py`) + task_runs 幂等 +
  grace 超时守卫 + 归因 + web 看板。

## 二、风险清单 (代码取证, 按严重度)

### P0 — 数据层是最大风险源 (test-v305 三连实证)

1. **`_cs_zscore` 共享层不过滤 inf** (`quant/factor/registry.py`):
   单只 inf → std=NaN → 全 universe 截面 NaN → 0 行。ctr_20d 已在因子侧
   防住 (test-v305), 其余 60+ 因子裸奔。修复 = isfinite 过滤 + 测试。
2. **数据停滞无告警**: fund_flow 停于 2026-02-27, margin 停于 2026-07-09
   (factor_values 实证)。两因子在池每晚白算 134 日期, 无人发现。
   缺每表新鲜度 SLO 监控: last_date vs 最近交易日 → 告警 + 受影响因子清单。
3. **物化池不感知数据可用性**: is_materialized 永 False → 每晚 ~30min
   死算不可物化因子。应按依赖/可用性裁剪池。
4. **基本面前视嫌疑 (比 v305 更隐蔽)**: `get_fundamentals` 的 stocks 快照列
   (roe/eps/bvps/high_52w) 每日覆盖写入无历史 — 用这些列的因子物化历史日期
   时拿到"今天的值回填昨天" = 前视。daily_valuation 按日 pe_ttm 部分缓解,
   覆盖深度未验证。若坐实, 基本面因子历史段全部需重算 → 升 P0。

### P1 — 数据质量债

5. turnover 2026-07-10 前近全零 (实测每日 ~35 只有值) → abn_turnover /
   ctr_20d / hl_volume_20d 历史段空。`backfill_turnover()` 存在但未跑全历史。
6. factor_values 单因子 COUNT 查询 30-60s 级 (全扫) → 索引审计。
7. `ideal_amplitude` 逐股 python 循环 (5208×134); `preload_ztd_cache`
   O(dates×rows) 过滤 (91s/134 dates) — 可向量化。

### P2 — 架构方向

8. **无真实券商接入**: ExecutionEngine 纯模拟写 trades.db。若目标实盘:
   按 LEAN 模式抽象 Brokerage 接口, 先纸面后 miniQMT; 若定位信号服务,
   维持现状但 README 写明。
9. hyperopt (Optuna, `optimizer/hyperopt.py`) 是否走 purged CV 未验证 —
   若否, 与 CPCV/DSR 体系自相矛盾, 是过拟合源。
10. DataStore 2048 行 god-object, repos/ 拆分过半 — 继续收口。

## 三、路线图

- **本周**: P0-1 + P0-2 (watchdog + 修两源) + P0-3 — 小改动, 消掉每晚
  30min 浪费和静默数据腐烂。
- **2-4 周**: P0-4 (PIT 审计, 若坐实升 P0) + P1-5 + P1-6/7。
- **1-2 月**: 因子 tear sheet 自动化 (对齐 Alphalens: 分位组合收益/换手/
  衰减进 weekly 评估); hyperopt CV 审计。
- **长期**: DataStore 拆完; 因子依赖 manifest (每因子声明依赖表 → 物化池/
  告警/门禁全自动) — qlib 数据服务化核心思想, P0-2/3 的制度化终态。

## 四、执行核对表

| 项 | 状态 | 证据 |
|---|---|---|
| P0-1 _cs_zscore isfinite 防腐 | ✅ | registry.py isfinite 过滤 + reindex 保索引契约; test_factor_compute +3 用例; 全套 166 绿 |
| P0-2 数据新鲜度 watchdog + 修 fund_flow/margin | ✅ 代码 / ⏳ 回填 | freshness.py SLO 5 表 + daily_data 接入 fund_flow/margin/valuation 同步 + send_alert; 根因修复: 东财封 requests→curl 降级 (fund_flow.py), to_compact 漏 import ×2 (margin.py/daily_sync.py — margin 停滞根因); 172 绿 |
| P0-3 物化池按数据可用性裁剪 | ✅ | freshness.unavailable_factors (TABLE_TO_FACTORS) + factor_cache 裁剪, 源恢复自动回池; 172 绿 |
| P0-4 基本面 PIT 审计 | ✅ 审计+修复 / ⏳ 回填 | 坐实: get_fundamentals 覆盖外回退快照=前视 (删 26 万污染行 ep_ratio/bp_ratio/size/roe_ratio @07-06..07-24); 修: 严格 PIT 只认 ≤date 最近 daily_valuation; high52w_dist 原已 PIT (误判纠正); daily_valuation 接晚间链 + jq 异常 tushare 兜底 + 限流重试; financial_* 无 ann_date (60d 近似, 年报窗口有界前视) + 停滞 2025-12-31 → 列入 P1; 175 绿 |
| P1-5 turnover 全历史回填 | ⬜ | |
| P1-6 factor_values 索引审计 | ⬜ | |
| P1-7 ideal_amplitude/ztd 向量化 | ⬜ | |
| P2-8 broker 抽象界定 | ⬜ | |
| P2-9 hyperopt purged-CV 审计 | ⬜ | |
| P2-10 DataStore 收口 (界定范围) | ⬜ | |

## 五、审计后新增 (执行中发现)

| 项 | 状态 | 说明 |
|---|---|---|
| financial_* ann_date 列 + 公告滞后精确化 | ⬜ P1 | jq 提供 ann_date 但未入库; 当前 stat_date≤date-60d 近似, 年报 (120d 法定期) 2-4 月窗口有 ≤2 月前视 |
| financial_* 2026Q1/Q2 同步 | ⬜ P1 | 数据止于 2025-12-31; 需 jq 凭据 (JQDATA_USER/PASS) 或替代源 |
| daily_sync.py 同样漏 to_compact import | ✅ | margin 每晚静默崩 = 停滞根因 (非数据源问题) |
| tushare daily_basic 限流 1次/min 重试 | ✅ | 62s 退避 ×6 |
