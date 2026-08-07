# 全量代码审查报告 2026-08-07

> 审查范围：`quant/` 164 个 Python 文件 / 36,632 行（config/utils/core/data/factor/alpha/risk/optimizer/execution/backtest/evaluation/regime/scheduler/monitor/benchmark）+ `web/` + `test/`。
> 只审查代码，未依据任何文档。
> 结论：架构蓝图清晰、分层合理，数据层幂等建表/多源回退、因子层短路预计算、缓存 sha256 失效等设计扎实；但 **"骨架完整、血管多处堵塞"**：共发现 ~90 个问题，其中 **P0 级 12 个**（必然崩溃/静默算错/链路全断），回测↔实盘↔评估三大闭环各有断裂点。

---

## 1. 技术选型适配性与改进方向

**基本合适**：Python + SQLite + pandas/numpy + scipy + LightGBM/XGB + hmmlearn + Flask，对 ¥5000 单机个人项目是合理选择。SQLite 扛 2.9GB 行情 + 1.5GB 因子缓存，单机够用。

### 缺口与建议

| 问题 | 现状 | 建议 |
|---|---|---|
| 跨进程状态同步 | `web.state_broker` 是进程内单例，pipeline 与 web 是两进程，**SSE 推送实际断裂**（state_broker.py:21 注释自相矛盾） | 恢复 HTTP POST 桥，或 web 从 DB 读 + 轮询（最简单） |
| 告警通道 | `notify.py` 整文件死代码，telegram token 空（config.yaml:379），回撤/失败告警永不触发 | 接一个通道（Telegram 最便宜），或至少落盘 + 日报 |
| 交易日历 | `calendar.py:28-43` 手工维护节假日，2026 端午"待确认" | 引入 `exchange_calendars` 库，或拉取交易所日历接口 |
| 数据源生命周期 | tencent/akshare 已被封禁仍在回退链上每批白烧 12-25s（store.py:1995-2005）；北向死源每天空转 25min（northbound.py） | 从回退链摘除/加开关 |
| 大量批处理 | 逐行 INSERT 遍布 10+ 模块（news/margin/northbound…） | `executemany`，5 万行级可提速 10x |
| ML 能力 | LGB 每晚训练但**预测从未被 pipeline 调用**（hyperopt 搜索空间不含 lgb/xgb，model.py:118-148 回退逻辑不可达） | 要么接线并修训练 bug，要么关闭训练省资源 |
| 测试基建 | web/benchmark/monitor/qlib_model 零测试 | 补 P0 相关回归测试 |

**不建议加**：Redis、Prometheus、Docker——单机单用户是过度工程（模板 8 单人单机限定）。

---

## 2. 已实现 vs 待实现

### 已实现（功能面完整）

- 数据：8 源日线回退链、复权体系、基本面 PIT、两融/北向/新闻/龙虎榜/质押/股东/宏观/分析师/资金流 15+ 数据域
- 因子：109 个注册因子（72 价格 + 37 基本面），物化缓存 + 源码哈希失效，IC/边际评估，四态状态机，因子策展
- Alpha：sleeve / ic_weighted / LGB / XGB / regime 组合（5 种合成方式）
- 风控：行业/市值中性化、Ledoit-Wolf、约束过滤、VaR/CVaR、压力测试
- 组合：Nano/Micro/Small 三层、Kelly、HRP、成本带
- 执行：成本模型、T+1、硬止损（实盘另有 ATR 三重止盈止损）
- 回测：walk-forward、MTM 净值、全套指标
- 评估：8 阶段流水线、CPCV、DSR、PBO
- 调度：晚间链 + orchestrator 状态机 + 5s 监控 + 快照 + 对账
- Web：21 路由仪表盘、SSE、OpenAPI、健康检查（缺陷见 §5）

### 待实现 / 断裂的功能

- **实盘券商对接**：只有 `SimulatedAdapter`，无真实券商通道
- **归因闭环**：Brinson 归因 Rp==Rb 同源（attribution.py:116-126）→ 选股效应恒为 0；factor_attribution 单位错 100 倍（:142）
- **基准闭环**：benchmark_tracking 只写不读，rolling alpha/IR 全 NULL，web 无任何基准端点
- **数据 gap 自动回补**：某晚断电则 daily 永久缺一天（daily_data.py:28 只拉当日）
- **估值连续性**：2026-04~05 窗口 JQData trial 停 + tushare 无权限，若 em_valuation 未回补则 EP/BP 因子大范围 NaN
- **资金流/新闻等 8 个数据源无调度入口**（freshness.py 自证 fund_flow 曾停 5 个月）

---

## 3. 业务逻辑是否清晰？是否闭环？

**设计上闭环**：晚间链 → 因子物化 → 信号 → 组合 → 执行 → 交易记账 → 归因 → 告警 → 次日反馈。**实现上有 7 个断点**：

1. **失败不重试**：evening.py:113-114 吞异常只 log，子进程退出码仍为 0，orchestrator.py:269-271 误判成功——"失败自动重试"从未兑现
2. **质量门禁形同虚设**：evening.py:63-71 数据质量 error 不阻断链
3. **回撤告警断链**：`record_daily_equity` 生产零调用（仅测试），get_max_drawdown 恒 0 → 回撤告警永不触发
4. **pipeline 异常告警断链**：`pipeline.errors` 计数器从未 inc（metrics.py 也无持久化）
5. **0 信号日误报 failed**：execute.py:55-59，全市场冷却/涨停的日子显示 execute FAILED
6. **lgb_train "skipped" 状态破坏状态机**：task_log 契约只有 ok/failed，skipped 会让整条晚间链标 failed（lgb_train.py:32-37）
7. **回测↔实盘行为不一致**：回测只模拟 8% 硬止损，实盘却有 ATR 三重止盈/锁利/时间止损（stop_loss.py:175-260 未接入 execution_model.py）——回测结论对实盘无代表性

---

## 4. 架构是否需要优化 / 重构？

**需要，但建议"外科手术式"而非推倒重来**：

- **依赖方向倒置（最重）**：`quant/pipeline.py:129`、`scheduler/monitor.py`、`monitor/alerts.py` 反向 import `web.state_broker`，核心层倒挂 Web 层。应先做「状态写入方」抽象（DB 为真相源）
- **生产服务**：Flask dev server 单进程跑生产（app.py:929-932），建议 gunicorn 或保持现状但明确单用户定位
- **因子层违反自身架构**：fundamental.py 15 处直接连 DB，与"纯函数"docstring 正面冲突（_shared_limit_conn 被设 None 后全部重开连接）；ihn/pledge/holder 等未走 aux 预加载，每晚重复全表扫描
- **死代码/死文件量大**：state_pusher.py（全文件）、app.py.tmp（旧版已入库）、index_fix.py（危险残留，误执行会回退旧逻辑）、rotation.py、risk/atr.py、market_microstructure.py、tca.py、impact.py 大部分、var.py 的 marginal_var/update_daily_risk（后者还含 NameError）
- **重复真相源**：schema.sql 与 store.py 双份 DDL；19 处 DB_PATH 常量；`compute_turnover_accel` 两份实现+一份短路表三处并存；换手约束在 portfolio 与 rebalance 各写一遍
- **两套 regime 仓位机制并存**：detector.py get_regime_sizing 与 portfolio.py _get_regime_max_lots（前者已废弃未删）
- **配置漂移**：两套 tier cap（optimizer.*_cap vs backtest.tier_*_cap）；constants.py 模块导入时冻结，`loader.override()` 热覆盖无效

---

## 5. 逻辑错误（P0 确认清单）

| # | 位置 | 问题 |
|---|------|------|
| 1 | phase2_single.py:143-145 vs phase3_oos.py:39/bridge.py:36/phase5:34 | **评估链路全断**：phase2 存 active/probation/archived，下游读 `passed` → candidates 恒空，Phase 3-8 级联空转 |
| 2 | phase8_live_consistency.py:111/208/316 | D1 NameError（factor_store 未定义）、D2/D4 TypeError（run_backtest 无 suppress_push 参数）→ 实盘一致性检查 3/4 维度必崩 |
| 3 | loop.py:144-160 | `_compute_dsr` 缺 n_obs 且传数组 → DSR 恒 None 被吞 |
| 4 | qlib_model.py:183-188 | `fillna(0)` 在 mask 之前 → 无收益股票被打 0 标签进训练集 |
| 5 | qlib_model.py:234-243 | 分块训练每块 200 棵树 → 总 2400 棵，"数学等价"注释错误，深度过拟合 |
| 6 | qlib_model.py:322-339 | 预测时缺列零填充放末尾 → 列序错位，全截面结果错乱 |
| 7 | fundamental.py:955-967 | `compute_dividend_yield` 实为"股息额"未除股价，price_rows 死查询 |
| 8 | store.py:2095-2100 | `get_daily` 缓存键只取前 200 符号 → 键碰撞返回错误数据 |
| 9 | store.py:1524-1532 | backfill 单位错误（volume 存股、turn 除 10000）→ 补数口径不一致 |
| 10 | kelly.py:110-130 | `kelly_raw/fraction` 被两次归一化抵消 → **熊市不缩仓，fractional Kelly 是空操作** |
| 11 | trade_repo.py:321-350 | get_cash 等忽略 mode 参数 → live/sim 资金串号 |
| 12 | execution_model.py:215-237 | 冲击价不更新 o.cost → 成交后现金可为负；且 5bps 冲击+0.1% 滑点双重计费 |

其余高频：`_apply_cost` 空订单 UnboundLocalError（:234-237）、Nano 层 0 手直接崩回测（portfolio.py:325-336）、`_iterative_clip` 后资金不补足（3 只股只投 15%）、rebalance.py:146 cash=0 时把总资产当现金、constraints.py:157 `_log_seal` 先使用后定义、TP1 半仓在 1 手持仓时变全仓、trail_sl 未要求盈利、执行日无涨跌停检查（可用一字板）、停牌 ffill 后可交易、multi_tf 周线 look-ahead（默认关闭但危险）、web 层 XSS（26 处 innerHTML 零转义）+ 无鉴权 POST /api/trade、/api/state。

---

## 6. 算法层面优化建议

1. **Kelly（P0）**：逐股 σ² 被全局标量替代后又归一化抵消，实际退化为 alpha 比例。修法：f* = (μ−r_f)/σ²_i 逐股、Fractional k 直接缩权不再归一化、用 IC 序列均值而非单点 med_ic
2. **LGB/XGB（P0 或下线）**：标签掩码修复、分块训练改累积学习率减半或一次全量、训练集/验证集切分、上线前 OOS IC 验证。更现实的选择：¥5000 账户 2-5 只股票，ML 能力严重过剩，建议**降级为研究工具**
3. **归因（P1）**：Brinson 的 Rp 必须用组合持仓收益而非行业收益（attribution.py:116-126 同源 bug 修完后才有意义）
4. **HRP（P2）**：簇风险用逆方差代理 + 簇内等权，偏离 De Prado 原文（应 IVP 簇内加权）；clip 5% 后风险平价性质被破坏
5. **Ledoit-Wolf（P1）**：任一 NaN 污染全矩阵（covariance.py:82-88），与 sample_cov 的 complete-pair 行为不一致，需统一 NaN 策略
6. **执行（P1）**：AC 冲击模型是死代码（cost.py:90 唯一调用方无人传 volume），要么接线要么删；回测补涨跌停成交模拟 + 执行日检查
7. **回测口径（P1）**：qfq 无现金分红记账、停牌 ffill 提供虚假流动性、phase7 训练窗口失效造成**结构性前读**（因子选择看到未来）——三项直接影响结论可信度
8. **HMM（P2）**：backtest 用 `_bm_rets*100` vs live 未缩放，两处特征量纲不一致；60 天截断窗口滤波丢历史先验
9. **因子合成（P2）**：sleeve 模式 mean-rank 被未入选因子稀释（synth.py:141-146）、ic_weighted 双标准化——建议统一先标准化再按 |IC| 加权

---

## 结论与修复顺序建议

技术栈合适；**问题不在选型，而在 12 个 P0 bug 和 3 条断链（评估链/告警链/SSE）**。修复顺序：

1. **P0 正确性**（#1-12）：评估链接、Phase8 参数、DSR、Kelly、dividend_yield、缓存键截断
2. **闭环修复**：SSE 跨进程、告警三条链、失败重试、lgb skipped 状态
3. **回测可信度**：phase7 前视、涨跌停/停牌模拟、回测↔实盘止损一致性
4. **清理**：死代码集中拆除、依赖倒置、DB_PATH 收敛 + 配置文件单源收敛