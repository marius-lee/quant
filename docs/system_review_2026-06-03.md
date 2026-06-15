# Quant 系统全面 Review — 2026-06-03

## 北极星目标: ¥5000 → ¥100万 / 6个月

---

## 一、已实现功能

### 数据层
- A股全量日线 (~5500只, SQLite market.db 396MB)
- 多数据源 fallback: tickflow → zzshare → tushare → akshare → tencent
- 基本面同步 (腾讯财经 PE/PB/市值, 60只/批)
- 龙虎榜数据 (lhb_detail 表)
- float32 存储 + WAL 模式 + check_same_thread=False

### 因子层
- 50+ 手工因子 (Polars 加速): momentum/reversal/volatility/MA/deviation/volume/illiquidity/PIN/herding/HL/Nash
- 100 自动 alpha 因子 (WorldQuant 风格, 随机组合算子)
- 妖股信号 (量能30%+突破30%+波动20%+连涨10%+动量10%)
- 涨停板模式识别 (首板/二板/N板/一字板/T字板/炸板 + 次日溢价)
- 涨停确认器 (三阴底/半量密码/四星蓄势/上下试探)
- 市场情绪 5 阶段 (冰点/衰退/复苏/扩张/高潮 + 仓位系数)
- TOI 隔夜-日内拉锯因子
- 龙虎榜因子 (出现次数/净买额/综合得分)
- IC 衰减监控 (df_std 活跃度代理, healthy/decaying/dead)

### 策略层
- 双策略并行: 因子选股 (稳健) + 涨停追击 (激进)
- ML集成: LightGBM + XGBoost + ExtraTrees (串行训练, OOM 安全)
- 尾部IC(70%)+全局IC(30%)加权, 负IC模型自动排除
- 5 阶段规划器: 5000→1万→5万→20万→50万→100万 (动态 ml_weight/neutralize/top_n/max_drawdown)
- 交易规则引擎: 买入条件 (score≥0.8, 非冰点, 5日涨幅<9.5%) + 卖出条件 (TP+15%, SL-5%, 时间止5天, 移动止损)

### 引擎层
- IC 日期流式筛选 (50天/批 × 全量股, float32, ~100MB 峰值, 全局rank精确等价)
- 模型训练 (train_window 252天, 因子预加载复用)
- 分批预测 (500只/批, eval_date 防前视)
- ranker: ML+妖股条件加成(threshold 0.3) + 涨停板模式 5% + 可选中性化
- 向量化回测: 涨跌停过滤 + 5000元约束 + 真实佣金(最低5元) + 手数取整 + 移动止损
- Walk-forward 回测 (已实现, 未接入主管线 — 见下方分析)

### 执行层
- 半自动实盘券商 (订单生成→人工执行→成交记录→持仓追踪, live.db)
- 风控: 止损/熔断/集中度/日亏损限制/连续亏损检测
- Paper trading (信号记录 + 次日评分 + 累计收益)
- T+1 纸上验证 (推荐价 vs 次日开盘/最高/收盘)
- A股交易日历 (akshare + 2025-2026 节假日硬编码 + 中文交易时段)
- 实时行情 (新浪财经, 60只/批)
- 告警系统 (signal/order_ready/stop_loss/daily_loss/anomaly/fill_reminder)

### Web 层
- Flask SPA (端口 8521), PWA 支持
- 4 页面: 推荐/持仓/盈亏/系统状态
- 20+ API 路由
- 暗色主题, 红涨绿跌, sparkline SVG
- Service Worker 离线缓存

### 运维层
- 3 launchd 定时任务: server(常驻) + analysis(8:00/16:00) + end_of_day(15:15)
- 统一任务状态追踪 (task_status.json + task_log.md)
- OOM/stuck 检测 (detect_stuck)

---

## 二、架构评价

### 优点
1. **层次清晰**: data → factor → strategy → engine → execution → web, 每层职责明确
2. **M1 8GB 可运行**: float32 + 日期流式IC + 串行训练 + shared raw_daily 缓存, 峰值 ~600MB
3. **模块化好**: 因子/策略/回测均可独立测试, BaseStrategy ABC 统一接口
4. **配置驱动**: config.yaml + ${ENV_VAR} 替换, 阈值/开关集中管理

### 需改进
1. **save_result() 副作用过重** (web/db.py:68-108): 保存结果同时触发 sim_broker + paper trading + DataStore 新建。应拆成独立 hook。
2. **服务器启动强制写 config** (web/app.py:31-37): 每次重启覆盖 strategy.active → factor。应仅当缺失时写默认值。
3. **回测管线 = 静态单点预测 + buy-and-hold**: 与对外声称的"周频调仓"存在根本差异 (详见下方分析)

---

## 三、Walk-Forward 未接入原因分析

### walkforward.py 做了什么
- 滚动窗口: train_window=252天, step=21天
- 每窗口: 重训模型 → 预测 → 等权持仓 → 计算该窗口指标
- 拼接所有窗口样本外收益 → 完整 OOS 权益曲线

### 不接入的 5 个理由

**1. 内存约束 (M1 8GB)**
Walk-forward 每个窗口需要 `factors_repo.load_batch(all_stocks, start, end)` 重新加载因子数据。11 窗口 × 252 天因子数据加载, 每窗口 ~100MB 因子 + 模型训练 internal copies = 峰值可能超过 8GB, 触发 OOM kill。当前 anchored split (train_split=0.92) 只加载一次因子数据, 峰值 600MB, 在 M1 8GB 内安全。

**2. 时间成本**
11 窗口 × 3 模型串行训练 (~60s/窗口) = ~11 分钟额外耗时。加上数据同步 + 因子计算 + 双策略 = 总耗时 ~15-18 分钟。8:00 的定时任务在 9:15 开盘前必须完成, 15+ 分钟没有安全余量。

**3. 北极星目标匹配**
5000→100万 的核心是找到短期爆发力最强的信号 + 严格交易纪律, 而非构建"跨市场周期稳健"策略。Walk-forward 验证的是策略稳定性 (机构级需求), anchored split (6年训练 + 6个月测试) 验证的是未来 6 个月的 out-of-sample 表现, 恰好与我们的冲刺周期吻合。

**4. walkforward 代码本身也是静态持仓**
walkforward.py 每窗口也是 buy-and-hold (选 top N → 持仓到窗口结束), 并没有实现周频再平衡。所以它和 anchored split 之间的实际区别只是"模型重训次数", 不是"调仓频率"。两者对外汇报的"周频调仓"都不准确 — 真正的周频调仓需要在每个窗口内按周重新选股。

**5. 边际收益低**
当前 anchored split (train_split=0.92): 训练 1426 天, 测试 124 天 (~6 个月)。如果模型在这 124 天 OOS 上表现好, 大概率未来 6 个月也能表现好。Walk-forward 的 11 个 21 天窗口反而碎片化了验证信号 — 你看不到连续 6 个月的表现, 只看到一堆 21 天的片段。

### 结论
Walk-forward 保留为备选验证工具 (当需要排查模型是否过拟合某个特定时期时), 但不接入主管线。当前 anchored split 是 M1 8GB 约束下的最优解。

---

## 四、发现的逻辑问题

| # | 文件:行 | 问题 | 严重度 |
|---|---------|------|--------|
| 1 | `factor_strategy.py:99` | eval_date = test_dates_sorted[0] — 仅预测测试期第一天, 全程静态持仓 | 🔴高 |
| 2 | `backtest_runner.py:162-163` | top_stocks = pred.head(top_n) — 静态选股, 全测试期不变 | 🔴高 |
| 3 | `risk_checker.py:117-122` | pnl_pct 单位推断脆弱: abs>1 时盲目除以100 | 🟡中 |
| 4 | `risk_checker.py:209-217` | 连续亏损检测: ORDER BY id DESC 不保证时间顺序 | 🟡中 |
| 5 | `live_broker.py:333` | max_shares<100 → 强设100, 可能超预算 | 🟢低 |
| 6 | `builder.py:39-41` | missing_price 重复查 DB, raw_daily 缓存未传入 builder | 🟢低 |
| 7 | `web/db.py:95` | save_result 每次都 new DataStore(), WAL 膨胀 | 🟢低 |

---

## 五、待实现功能（按北极星优先级）

| # | 功能 | 优先级 | 理由 |
|---|------|--------|------|
| 1 | 周频再平衡回测 (真正按周重选股) | 🔴 | 当前静态持仓与实际策略不符 |
| 2 | 涨停板买入可行性约束 | 🔴 | 回测假设涨停可买, 实际买不到 |
| 3 | 按市值分层滑点 | 🟡 | 小盘股滑点 3-5% vs 当前固定 1% |
| 4 | 开盘后自动通知 (macOS 推送) | 🟡 | 涨停追击时效性要求 |
| 5 | 组合优化 (风险平价 / 最小方差) | 🟢 | 大资金阶段需要 |
| 6 | 多周期信号叠加 (日线+60min+15min) | 🟢 | 提升入场精度 |
