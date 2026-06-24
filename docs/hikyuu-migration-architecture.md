# Hikyuu 迁移架构设计 — 2026-06-24

## 总则

**原则**: 不丢弃核心资产（陈小群模式识别 + 因子日历因子 + 实时行情），用 Hikyuu 替代手工实现的框架性代码。
**策略**: 渐进迁移，每次迁移一个组件，用回测验证后再继续。

---

## 一、现有代码盘点 (30文件, 5782行)

### 逐文件决策

| # | 文件 | 行 | 决策 | 说明 |
|---|------|-----|------|------|
| 1 | `.claude/hooks/guard.py` | - | ✅ 保留 | Claude Code 钩子，无关 |
| 2 | `backtest/__init__.py` | ~10 | ❌ 废弃 | Hikyuu SP(Slippage) 替代 |
| 3 | `config/loader.py` | ~50 | ✅ 保留 | YAML 配置，与 Hikyuu 互补 |
| 4 | `data/__init__.py` | 0 | ✅ 保留 | 空 init |
| 5 | `data/store.py` | ~300 | 🔄 改造 | 保留 Sina 抓取逻辑, 输出 HDF5 |
| 6 | `data/trade_repo.py` | ~100 | ✅ 保留 | 股票过滤工具函数 |
| 7 | `execution/__init__.py` | 0 | ✅ 保留 | 空 init |
| 8 | `execution/calendar.py` | ~50 | ✅ 保留 | 交易日历工具 |
| 9 | `execution/quote.py` | ~400 | 🔄 迁移 | 模式识别 → crtSG |
| 10 | `execution/sell_chain.py` | ~120 | 🔄 迁移 | B1-B7 → crtST/crtTP |
| 11 | `factor/__init__.py` | 0 | ✅ 保留 | 空 init |
| 12 | `factor/market_mood.py` | ~170 | 🔄 迁移 | 情绪周期 → crtEV |
| 13 | `intraday_runner.py` | ~900 | ❌ 废弃 | Hikyuu PF_Simple 替代主循环 |
| 14 | `ops/hikyuu_poc.py` | ~550 | ✅ 保留 | POC 参考代码 |
| 15 | `ops/liquidity.py` | ~80 | 🔄 迁移 | 波动率分解 → 自定义 IndicatorImp |
| 16 | `ops/performance.py` | ~500 | 🔄 迁移 | Kelly → crtMM, MCVA → crtTP |
| 17 | `ops/position_sizers.py` | ~50 | ❌ 废弃 | Hikyuu MM 替代 |
| 18 | `ops/review.py` | ~200 | ✅ 保留 | 盘后复盘脚本 |
| 19 | `ops/sector_scan.py` | ~100 | ✅ 保留 | 月度板块扫描 |
| 20 | `ops/signal_algo.py` | ~60 | 🔄 迁移 | zscore/MA逆序 → 自定义 Indicator |
| 21 | `strategies/base.py` | ~120 | ❌ 废弃 | Hikyuu System 替代 |
| 22 | `strategies/etf_rotation.py` | ~50 | ✅ 保留 | 参考策略，日后迁移 |
| 23 | `strategies/market_timing.py` | ~50 | ✅ 保留 | 参考策略 |
| 24 | `strategies/smallcap_rotation.py` | ~50 | ✅ 保留 | 参考策略 |
| 25 | `utils/__init__.py` | 0 | ✅ 保留 | 空 init |
| 26 | `utils/date.py` | ~30 | ✅ 保留 | 日期工具 |
| 27 | `utils/logger.py` | ~30 | ✅ 保留 | 日志工具 |
| 28 | `web/app.py` | ~500 | ✅ 保留 | Flask 实时监控 |
| 29 | `web/shared.py` | ~50 | ✅ 保留 | 线程安全状态 |
| 30 | `web/static/*` | - | ✅ 保留 | 前端资源 |

**汇总**: 保留 17 文件 / 迁移改造 9 文件 / 废弃 4 文件

---

## 二、新架构顶层设计

```
┌─────────────────────────────────────────────────────────┐
│                   数据层 (Data Layer)                     │
│                                                          │
│  Sina 实时行情 → data/store.py                           │
│    ├── 盘中: 内存缓存 (dict)                              │
│    ├── 日线: → Hikyuu HDF5 (sh_day.h5, sz_day.h5)       │
│    └── 5分钟线: → Hikyuu HDF5 (sh_5min.h5, sz_5min.h5)  │
│                                                          │
│  market.db (SQLite, 2GB)                                 │
│    ├── stocks表 → Hikyuu stock.db 元数据                  │
│    ├── daily表 → 导出一份到 HDF5 (新), 保留作查询         │
│    └── 保留: trades.db (交易记录), results.db (回测结果)  │
└────────────────────────────┬────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────┐
│                   因子层 (Factor Layer)                   │
│  自定义 IndicatorImp:                                     │
│    ├── DownsideVol (下行波动率, 因子日历 page 53)         │
│    ├── OvernightGap (隔夜跳空, 因子日历 page 282)        │
│    ├── AmihudLiquidity (非流动性, 因子日历 page 36)       │
│    └── SkewnessProxy (偏度代理, 因子日历 page 91)        │
│                                                          │
│  FactorSet → MF_ICIRWeight (ICIR加权合成)                │
└────────────────────────────┬────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────┐
│                   策略层 (Strategy Layer)                 │
│                                                          │
│  Hikyuu Strategy (替代 intraday_runner.py)               │
│  ┌────────────────────────────────────────────────┐     │
│  │ EV (crtEV): 市场情绪周期                        │     │
│  │   迁移自: factor/market_mood.py                 │     │
│  │   _calculate 中计算涨停家数→情绪阶段→仓位系数    │     │
│  ├────────────────────────────────────────────────┤     │
│  │ MF (MF_ICIRWeight): 因子合成                   │     │
│  │   4因子 × ICIR加权 → 截面评分                   │     │
│  ├────────────────────────────────────────────────┤     │
│  │ SE (SE_MultiFactor2): 选股过滤                  │     │
│  │   因子评分 TopN + SCFilter(价格/金额/ST过滤)    │     │
│  ├────────────────────────────────────────────────┤     │
│  │ System (per-stock): 个股交易系统                │     │
│  │ ┌──────────────────────────────────────────┐   │     │
│  │ │ SG (crtSG): 陈小群模式识别                │   │     │
│  │ │   迁移自: execution/quote.py              │   │     │
│  │ │   S1弱转强(0.90) / S2首阴反包(0.85)      │   │     │
│  │ │   S3连板接力(0.70) / S4首板试探(0.50)    │   │     │
│  │ ├──────────────────────────────────────────┤   │     │
│  │ │ ST (crtST): 波动率自适应止损              │   │     │
│  │ │   迁移自: execution/sell_chain.py B1      │   │     │
│  │ │   adaptive_pct = 0.05 × (个股下行波/0.30) │   │     │
│  │ ├──────────────────────────────────────────┤   │     │
│  │ │ TP (crtST): MCVA 动态止盈                │   │     │
│  │ │   迁移自: ops/performance.py              │   │     │
│  │ │   MCVA = entry_α - current_α - MCAR_term │   │     │
│  │ ├──────────────────────────────────────────┤   │     │
│  │ │ MM (crtMM): 半Kelly 资金管理             │   │     │
│  │ │   迁移自: ops/performance.py              │   │     │
│  │ │   f = (b×p-q)/b × 0.5 × drawdown_scale   │   │     │
│  │ └──────────────────────────────────────────┘   │     │
│  └────────────────────────────────────────────────┘     │
│                                                          │
│  PF (PF_Simple): 多标的组合管理                          │
│    tm=TradeManager, se=SE_MultiFactor2, af=AF_EqualWeight│
└────────────────────────────┬────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────┐
│                   展示层 (Web Layer)                      │
│  Flask (web/app.py) ← 保留不变                           │
│  Hikyuu TradeManager → web/shared.py ← 读取状态          │
│  PySide6 GUI → 回测可视化 (新增, 仅研究用)               │
└─────────────────────────────────────────────────────────┘
```

---

## 三、组件映射详解

### 3.1 数据层：保留抓取，替换存储

```
现有: Sina API → data/store.py → market.db (SQLite, 2GB)
新:   Sina API → data/store.py → Hikyuu HDF5 (sh_day.h5, sz_day.h5)

迁移方案:
  1. 保留 data/store.py 的 Sina 抓取逻辑 (完整保留)
  2. 新增 store.to_hdf5() 方法，将日线写入 Hikyuu 兼容的 HDF5
  3. market.db 保留为元数据和实时数据缓存
  4. Hikyuu StockManager 指向 ~/stock/ 目录 (HDF5 + stock.db)
```

### 3.2 因子层：自定义 Indicator，因子日历落地

```
现有: 无因子计算框架 (手动 pandas 计算)
新:   Hikyuu IndicatorImp 子类 → FactorSet → MF_ICIRWeight

新增文件:
  hikyuu_factors/
    ├── downside_vol.py     # 下行波动率 (因子日历 page 53)
    ├── overnight_gap.py    # 隔夜跳空 (因子日历 page 282)
    ├── amihud.py           # 非流动性 (因子日历 page 36)
    └── skewness.py         # 偏度代理 (因子日历 page 91)

使用方式:
  factor_set = FactorSet([...])
  mf = MF_ICIRWeight(factor_set, stocks, query, ic_n=5, ic_rolling_n=120)
  se = SE_MultiFactor2(mf, topn=10)
```

### 3.3 信号层：模式识别保持，套 Hikyuu 壳

```
现有: execution/quote.py — BoardTracker.scan_all_modes()
新:   crtSG(_calculate) 封装同一逻辑

迁移:
  1. 提取 BoardTracker 中的模式判定逻辑 (纯函数)
  2. 封装到 crtSG 的 _calculate 方法
  3. SG 返回信号分 (S1=0.90, S2=0.85, S3=0.70, S4=0.50)
  4. 信号分经 MF 因子乘数调整 → SE 排序 → PF 执行

保留: Sina 实时行情获取 (quote.py 中的 WebSocket/HTTP 部分)
```

### 3.4 风控层：B1-B7 重构为 ST/TP 组件

```
现有: execution/sell_chain.py — 责任链模式
新:   Hikyuu ST (止损) + TP (止盈) 独立组件

映射:
  B1 硬止损 -5%     → crtST("VolAdaptiveStop")     波动率自适应
  B2 尾盘炸板       → crtST("LateBreak")           保持逻辑
  B3 反复烂板       → crtST("RepeatedBreak")       保持逻辑
  B4 死亡换手       → crtST("DeathTurnover")       保持逻辑
  B5 缩量加速       → crtST("ShrinkAccel")         保持逻辑
  B6 MCVA           → crtST("MCVATakeProfit")      作为 TakeProfit
  B7 z-score峰值    → crtST("ZScorePeak")          保持逻辑
  B8 时间止         → crtST("TimeStop")            保持逻辑

组装: System(..., st=复合ST, tp=MCVA)
```

### 3.5 资金层：Kelly 迁移到 Hikyuu MM

```
现有: ops/performance.py — kelly_fraction() 函数
新:   crtMM(get_buy_num) — 封装 Kelly 逻辑

迁移:
  1. 提取 kelly_fraction 核心公式
  2. 封装到 crtMM 的 get_buy_num 方法
  3. MM 从 TradeManager 获取当前资金、从 ST 获取每股风险
```

### 3.6 环境层：情绪周期迁移到 EV

```
现有: factor/market_mood.py — detect_mood() → stage + coefficient
新:   crtEV(env_calculate) — 封装情绪检测

迁移:
  1. 将 detect_mood() 逻辑封装到 EV 的 _calculate
  2. EV 控制: System 是否允许交易 + 仓位系数
  3. 冰点 → coefficient=0.15 → 缩小 MM 仓位
```

---

## 四、主循环替换

```
现有 (intraday_runner.py):
  while True:
    检测状态 (休市→盘前→盘中→午休→收盘)
    拉取实时行情
    扫描信号
    检查卖出
    执行买入
    更新前端

新 (Hikyuu Strategy + PF):
  Hikyuu Strategy:
    - on_bar() 每分钟触发 → 拉取实时行情
    - PF_Simple.run() → 自动处理: 选股→调仓→风控

  保留的部分:
    - 状态机 (休市/盘前/盘中) → Hikyuu StrategyContext
    - WebSocket/Sina 行情 → 独立线程, 写入 Hikyuu
```

---

## 五、文件组织方案

```
project/quant/
├── hikyuu_app/              # 新: Hikyuu 应用代码
│   ├── __init__.py
│   ├── factors/             # 自定义因子 Indicator
│   │   ├── downside_vol.py
│   │   ├── overnight_gap.py
│   │   ├── amihud.py
│   │   └── skewness.py
│   ├── signals/             # 陈小群信号
│   │   └── chen_signal.py   # 封装 crtSG
│   ├── stops/               # 止损止盈
│   │   ├── adaptive_stop.py # 波动率自适应
│   │   └── mcva_tp.py       # MCVA 止盈
│   ├── moneymgmt/           # 资金管理
│   │   └── kelly_mm.py      # 半Kelly
│   ├── environment/         # 市场环境
│   │   └── mood_ev.py       # 情绪周期
│   └── strategy.py          # 主策略组装 (EV+MF+SE+PF)
│
├── execution/               # 保留, 改造
│   ├── quote.py             # 保留 Sina 行情 + 模式检测
│   └── sell_chain.py        # 废弃 (逻辑迁移到 hikyuu_app/stops/)
│
├── factor/                  # 保留 (market_mood.py 迁到 hikyuu_app/environment/)
│   └── market_mood.py       # 保留为参考
│
├── intraday_runner.py       # 废弃 (Hikyuu Strategy 替代)
│
├── ops/                     # 保留
│   ├── hikyuu_poc.py        # POC 参考代码
│   ├── performance.py       # 保留 Kelly/MCVA 参考实现
│   ├── review.py            # 保留
│   ├── sector_scan.py       # 保留
│   └── signal_algo.py       # 保留 算法参考
│
├── data/                    # 保留 + 改造
│   ├── store.py             # 保留 Sina 抓取, 新增 to_hdf5()
│   ├── minute_store.py      # 新增: Sina 5分钟线 → HDF5
│   ├── trade_repo.py        # 保留
│   └── trade_calendar.json  # 保留
│
├── ~/stock/                 # Hikyuu 数据目录 (外部)
│   ├── stock.db             # Hikyuu 元数据 (自动生成)
│   ├── sh_day.h5            # 上海日线 (store.py 导出)
│   ├── sz_day.h5            # 深圳日线
│   ├── sh_5min.h5           # 上海5分钟线 (minute_store.py 导出)
│   └── sz_5min.h5           # 深圳5分钟线
│
├── web/                     # 保留不变
│   ├── app.py
│   ├── shared.py
│   └── static/
│
├── strategies/              # 保留为参考
├── utils/                   # 保留不变
├── config/                  # 保留不变
│   └── loader.py
│
└── backtest/                # 废弃 (Hikyuu 内置替代)
```

---

## 六、迁移路线图 (6阶段)

| 阶段 | 内容 | 产出 | 验证标准 |
|------|------|------|---------|
| **P0a** 数据迁移 | store.py 新增 to_hdf5(), 导入1年日线 | Hikyuu 可读取 A 股日线 | `sm['sh000001'].get_kdata(Query(-100))` 成功 |
| **P0b** 分钟存储 | minute_store.py: Sina 5分钟线 → HDF5 | 分钟数据积累中 | 至少积累5个交易日, 无缺帧 |
| **P1** 因子落地 | 4个自定义 Indicator + FactorSet + MF_ICIRWeight | 因子截面评分可用 | MF.get_scores(date) 输出排序 |
| **P2** 信号迁移 | 陈小群模式识别 → crtSG | SG 组件跑通回测 | 单股票 System.run() 有买卖记录 |
| **P3** 风控迁移 | B1-B8 → ST/TP 组件 + 对比回测 | 自适应止损 vs 固定止损 | Sharpe/回撤对比 |
| **P4** 组装上线 | EV+MF+SE+PF 完整策略 | 全市场回测报告 | IC₁, Sharpe, 胜率优于当前系统 (IC₁=-0.07) |
| **P5** 双轨验证 | Hikyuu 系统与现有系统并行 | 实盘信号对比 | 30天信号一致性分析 |

---

## 七、关键决策 (已确认)

| # | 决策 | 结论 | 理由 |
|---|------|------|------|
| 1 | 存储格式 | ✅ SQLite → HDF5 | Hikyuu SQLite驱动仅支持元数据, 日线必须HDF5。market.db保留作元数据+交易记录 |
| 2 | 分钟数据 | ✅ 开始存5分钟线 | 因子日历23个高频因子需要分钟数据回测。Sina 5分钟线 ~300MB/年(HDF5压缩后) |
| 3 | 实盘执行 | ✅ 短期手动, 长期自研 | WonderTrader股票接口为空壳, 且需机构资质。Hikyuu broker接口可自研轻量封装 |
| 4 | Web前端 | ✅ Flask保留, PySide6仅回测用 | Flask已有500行成熟代码; PySide6用于sys.plot()一键出图 |

---

## 八、数据迁移详细方案 (P0)

### 8.1 现状

```
market.db (SQLite, 2GB)
├── daily 表: 16M行, 字段 symbol/date/open/high/low/close/volume/amount/turnover
├── stocks 表: 5525只, 字段 symbol/name/market/pe/pb...
└── trading_calendar 表: 2557条

~/.hikyuu/hikyuu.ini → 配置指向 ~/stock/
~/stock/stock.db       → Hikyuu元数据 (market/stocktypeinfo/stock/block表)
~/stock/sh_day.h5      → Hikyuu上海日线 (HDF5)
~/stock/sz_day.h5      → Hikyuu深圳日线 (HDF5)
```

### 8.2 迁移脚本

```
data/store.py 新增:
  store.to_hdf5(start_date=None, end_date=None)
    │
    ├── 从 market.db daily 表读取日线
    ├── 按市场 (SH/SZ) 分组
    ├── 写入 Hikyuu HDF5 格式 (sh_day.h5, sz_day.h5)
    └── 使用 h5py 库, ~30行代码

~/stock/stock.db 新增:
  INSERT INTO stock (marketid, code, name, valid, type)
  从 market.db stocks 表同步股票列表到 Hikyuu 元数据库
```

### 8.3 分钟数据存储

```
新增文件: data/minute_store.py
  ┌─ Sina 5分钟行情 ──→ 内存缓存 ──→ 每日收盘后 ──→ sh_5min.h5 / sz_5min.h5
  │   (实时)              (dict)      (定时任务)       (HDF5, 压缩)
  │
  └─ 因子日历高频因子 ←── 从 HDF5 读取 ←── 回测用
```

存储量估算 (M1 8GB 约束):
- 沪市5分钟线: ~300MB/年 (5500只 × 48条 × 250天, HDF5压缩后)
- 不影响磁盘: 当前可用 26GB, 年增 ~0.3GB
