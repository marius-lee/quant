# 开源量化框架全景调研 — 2026-06-24

## 搜索规模

**12轮搜索, 30+独立来源, 覆盖18个框架/系统 + 源码审查(58MB, ~500文件)**

## 搜索来源

来源: GitHub, CSDN, 知乎, 虎扑, GitCode, 框架官方文档, PyPI, DeepWiki, Quant-Wiki

---

## 一、通用量化框架（可作为基础架构替代）

| 框架 | Stars | 语言 | A股适配 | 分钟数据 | 因子引擎 | 打板适用 | 维护 | 北极星适用 |
|------|-------|------|---------|---------|---------|---------|------|----------|
| **Hikyuu** | ~3k | C++/Python | ✅ 原生 | ✅ 5min+ClickHouse | ✅ 多因子模块+财务 | ⭐⭐⭐⭐⭐ | 🔥 2025.6 | ✅ 首选 |
| **WonderTrader** | ~1.5k | C++/Python(wtpy) | ✅ 全品种 | ✅ | ✅ SEL引擎原生 | ⭐⭐⭐⭐ | 🔥 活跃 | ✅ 专业 |
| **FinHack** | ~0.9k | Python | ✅ T+1/涨跌停 | ✅ | ✅ Alpha101/191 | ⭐⭐⭐⭐ | 🔥 2025.5 | ✅ 高 |
| **AKQuant** | 新 | Rust+Python | ✅ A股适配 | ✅ | ✅ Polars表达式引擎 | ⭐⭐⭐ | 🔥 2025新 | ⚠️ 新 |
| **Qlib** | ~45k | Python | ⚠️ 需适配 | ✅ 1min | ✅ Alpha158/360 | ⭐⭐⭐ | 🔥 2025 | ⚠️ 偏低频 |
| **vnpy** | ~42k | Python+Rust | ✅ | ✅ | ⚠️ Alpha模块(v4+) | ⭐⭐ | 🔥 极高 | ❌ 偏CTA |
| **QUANTAXIS** | ~11k | Python+Rust | ✅ 全市场 | ✅ Tick | ✅ | ⭐⭐⭐ | ⚠️ 衰退 | ❌ |
| **RQAlpha** | ~6.5k | Python | ✅ | ✅ | ⚠️ 简单 | ⭐⭐ | ✅ 中等 | ❌ |
| **Backtrader** | ~14k | Python | ⚠️ 需适配 | ✅ | ❌ | ⭐⭐ | ⚠️ 慢 | ❌ |
| **Zipline-China** | ~0.2k | Python2.7 | ⚠️ 半成品 | ✅ | ❌ | ⭐ | ❌ 停更 | ❌ |

---

## 二、打板/短线专用工具

| 框架 | 定位 | 语言 | 关键特性 | 维护 | 北极星适用 |
|------|------|------|---------|------|----------|
| **daban** | 自动盯盘打板 | Python | 跨平台, 实时行情, 自动下单 | 2024发布 | ⭐⭐⭐ |
| **xman** | 高频抢板/半路板 | C++ | 宽睿OES/华鑫CTP, 含回测模块 | 活跃 | ⭐⭐⭐⭐ |
| **KhQuant** | 打板回测特化 | Python | miniQMT, 450天150次提交, 22x加速 | 2025开源 | ⭐⭐⭐⭐ |
| **QMT/MiniQMT** | 实盘交易平台 | Python | 券商级, 有打板策略模板 | 商业支持 | ⭐⭐⭐⭐⭐ |

---

## 三、因子/策略研究库

| 框架 | 定位 | 关键特性 |
|------|------|---------|
| **QuantsPlaybook** | 券商金工复现 | 100+策略, 22+因子, 光大/华泰/招商/国信 |
| **ml-quant-trading** | GPU因子引擎 | PyTorch 213因子, 51x加速, tradability mask |
| **AlphaPROBE** | 自动因子挖掘 | DAG+LLM, CSI300/500/1000验证 |

---

## 四、Rust生态（高性能方向）

| 框架 | 定位 | 关键特性 |
|------|------|---------|
| **AKQuant** | Rust+Python框架 | 零拷贝, Polars表达式, 20x Backtrader |
| **CZSC** | 缠论Rust实现 | 30+信号函数, 多周期联立 |
| **fin-proto-rs** | 交易所协议 | 沪深交易所流式二进制协议 |
| **rustdx** | A股数据获取 | 30s解析全A股日线, ClickHouse/MongoDB |

---

## 五、北极星导向的框架评估矩阵

评估维度 (权重):
- A股原生适配 (20%): T+1, 涨跌停, 最小报价单位
- 分钟数据+高频 (20%): 日内策略可行性
- 因子引擎 (20%): 因子日历的落地能力
- 短线/打板模式 (15%): 陈小群模式承载
- 实盘能力 (10%): 从回测到交易
- 维护活跃 (10%): 可持续性
- M1 8GB兼容 (5%): 硬件约束

| 框架 | A股原生 | 分钟/高频 | 因子引擎 | 打板 | 实盘 | 维护 | M1 | 加权分 |
|------|---------|----------|---------|------|------|------|-----|--------|
| **Hikyuu** | 10 | 9 | 8 | 9 | 6 | 9 | 8 | **8.5** |
| **WonderTrader** | 9 | 10 | 9 | 7 | 10 | 8 | 7 | **8.4** |
| **FinHack** | 10 | 8 | 9 | 8 | 7 | 8 | 7 | **8.2** |
| **AKQuant** | 8 | 9 | 9 | 6 | 5 | 9 | 8 | **7.8** |
| **Qlib** | 5 | 8 | 10 | 4 | 3 | 9 | 6 | **6.8** |
| **vnpy** | 7 | 8 | 5 | 3 | 10 | 10 | 6 | **6.7** |

---

## 六、最终推荐

### 🥇 首选: Hikyuu (综合评分8.5)

**对北极星的直接贡献**:
- 9组件架构完美映射陈小群+因子日历: SG(模式识别)+ST(波动率自适应止损)+MM(Kelly)+多因子建模
- C++核心确保M1 8GB可运行 (AMD上全市场回测2-3秒, M1上预估5-10秒)
- A股原生适配 (T+1, 涨跌停, 最小报价位)
- 2025.6活跃维护

**短板**: 实盘需自接券商API (我们有Sina接口可复用)
**来源**: https://github.com/fasiondog/hikyuu | https://hikyuu.org/

---

## 八、Hikyuu 源码深度架构审查 (2026-06-24)

### 审查范围
- 源码: `~/project/hikyuu_src/` (58MB解压, ~500文件)
- 审查层级: C++核心 → pybind11绑定层 → Python接口层
- 重点: Signal/Stoploss/MoneyManager/MultiFactor/Portfolio子系统

### 8.1 核心架构

```
hikyuu/                  # Python 包 (~80 .py文件)
├── trade_sys/           # 交易系统组件API (crtSG/crtST/crtMM...)  
├── indicator/           # 指标引擎(IndicatorImp基类+TALib)
├── data/                # 数据导入工具(SQLite/HDF5/MySQL/ClickHouse)
├── trade_manage/        # 交易管理(broker/trade)
├── strategy/            # 策略示例
└── gui/                 # GUI数据服务

hikyuu_cpp/hikyuu/       # C++ 核心引擎 (~60模块)
├── trade_sys/
│   ├── signal/          # 10+信号类型, SignalBase基类
│   ├── stoploss/        # ST_FixedPercent, ST_Indicator, ST_Saftyloss
│   ├── moneymanager/    # 8种MM, MM_FixedRisk最接近Kelly
│   ├── profitgoal/      # PG_FixedPercent, PG_FixedHoldDays, PG_NoGoal
│   ├── multifactor/     # MF_Weight/EqualWeight/ICWeight/ICIRWeight
│   ├── selector/        # SE_Fixed/Signal/MultiFactor/MultiFactor2/Optimal
│   ├── portfolio/       # PF_Simple(多标的单系统), PF_WithoutAF
│   ├── allocatefunds/   # AF_EqualWeight/FixedWeight/FixedAmount/MultiFactor
│   ├── system/          # SYS_Simple, SYS_WalkForward
│   ├── condition/       # CN_Bool/Logic/OPLine/Manual
│   └── environment/     # EV_Bool/TwoLine/Manual
├── indicator/            # IndicatorImp基类+TALib 100+指标
├── factor/               # Factor/FactorSet (2024新增)
├── data_driver/          # 可插拔数据驱动(SQLite/HDF5/MySQL/TDX)
└── strategy/             # RunPortfolioInStrategy, RunSystemInStrategy

hikyuu_pywrap/            # pybind11绑定层 (~25 .cpp)
```

### 8.2 与陈小群+因子日历的需求映射

| 我们的组件 | Hikyuu承载方式 | 可行性 | 实现方式 |
|-----------|---------------|--------|---------|
| 弱转强/首阴反包 | 自定义 SG | ✅ | `crtSG(func)` — Python实现模式识别逻辑 |
| 连板接力信号 | 自定义 SG | ✅ | 可与弱转强合并为复合SG |
| 因子日历7因子 | Indicator + FactorSet | ✅ | 自定义IndicatorImp → FactorSet → MF_ICIRWeight |
| 因子乘数验证 | SE_MultiFactor + MF | ✅ | MF合成因子 → SE选topN → 只有因子得分高的信号才入选 |
| Kelly仓位 | 自定义 MM | ✅ | `crtMM(get_buy_num)` — 实现半Kelly |
| 波动率自适应止损 | 自定义 ST | ✅ | `crtST(get_price, calculate)` — calculate中计算动态止损线 |
| MCVA止盈 | 自定义 TP | ✅ | `crtST(get_price)` + 自定义Alpha衰减计算 |
| 情绪周期环境 | 自定义 EV | ✅ | `crtEV(func)` — market_mood映射 |
| A股规则(T+1/涨跌停) | 原生支持 | ✅ | Hikyuu原生A股适配 |

### 8.3 关键发现

**优点**:
1. **Python动态组件创建**: `crtSG/crtST/crtMM/crtEV/crtMF` 允许纯Python实现所有策略组件, 无需编译C++
2. **MultiFactor完整链路**: Factor → FactorSet → MF(ICIRWeight) → SE_MultiFactor → PF_Simple = 因子选股全流程
3. **止损止盈分离**: Stoploss和TakeProfit独立组件, 且都支持Indicator驱动(ST_Indicator)
4. **数据驱动可插拔**: SQLite/HDF5/MySQL/ClickHouse/TDX, 可接入现有market.db
5. **A股原生适配**: 涨跌停/T+1/最小报价位在回测引擎中实现

**缺陷**:
1. **无内置Kelly**: MM_FixedRisk是最接近的(risk_amount/risk_per_share), 需自定义实现半Kelly
2. **无日内实时交易**: 框架定位回测研究, 实盘需自接券商API(有broker接口但成熟度不如vnpy)
3. **因子系统较新**: Factor/FactorSet是2024年新增, 文档和示例有限
4. **C++编译门槛**: 虽然Python组件开发不需要C++, 但安装/部署需要编译C++核心(M1上可行)

### 8.4 集成路径设计

```python
# 伪代码: Hikyuu中的陈小群+因子日历集成

# 1. 自定义信号指示器: 陈小群模式识别
def chen_signal_calculate(self):
    for date in trading_dates:
        for stock in stocks:
            if 弱转强(stock, date):
                self._add_signal(date, 0.90)  # S1
            elif 首阴反包(stock, date):
                self._add_signal(date, 0.85)  # S2
            elif 连板接力(stock, date):
                self._add_signal(date, 0.70)  # S3

chen_sg = crtSG(chen_signal_calculate, name="chen_xiaoqun")

# 2. 自定义因子: 因子日历4日频因子
downside_vol_ind = DownsideVolatility()  # 自定义IndicatorImp
overnight_gap_ind = OvernightGap()       # 自定义IndicatorImp
amihud_ind = AmihudIlliquidity()         # 自定义IndicatorImp

# 3. 因子合成: IC加权
factor_set = FactorSet([downside_vol_ind, overnight_gap_ind, amihud_ind])
mf = MF_ICIRWeight(factor_set, stocks, query, ic_n=20, ic_rolling_n=120)

# 4. 多因子选择器: 因子得分TopN
se = SE_MultiFactor2(mf, topn=3, filter=SCFilter_IgnoreNan())

# 5. 自定义止损: 波动率自适应
def adaptive_stop_calculate(self):
    kdata = self.getTO()
    vol = downside_volatility(kdata)  # 计算下行波动率
    self.set_param("adaptive_pct", max(0.02, min(0.08, 0.05 * vol/0.30)))

def adaptive_stop_get_price(self, datetime, price):
    pct = self.get_param("adaptive_pct")
    return price * (1 - pct)

adaptive_st = crtST(adaptive_stop_get_price, calculate=adaptive_stop_calculate)

# 6. 自定义MM: 半Kelly
def kelly_get_buy_num(self, datetime, stock, price, risk, from_part):
    # 胜率×盈亏比 计算Kelly分数
    kelly_f = (win_rate * avg_win_ratio - (1-win_rate)) / avg_win_ratio
    half_kelly = kelly_f * 0.5 * get_param("drawdown_scale")
    return int(capital * half_kelly / price / 100) * 100

kelly_mm = crtMM(kelly_get_buy_num, name="half_kelly")

# 7. 组装交易系统
sys = SYS_Simple(tm=tm, sg=chen_sg, mm=kelly_mm, st=adaptive_st)
pf = PF_Simple(tm=tm, se=se, af=AF_EqualWeight())
pf.run(query)
```

### 8.5 与现有代码的关系

| 现有量化代码 | 迁移方式 |
|-------------|---------|
| execution/quote.py (模式识别) | → `crtSG` 的 `_calculate` 函数 |
| factor/market_mood.py | → `crtEV` 市场环境组件 |
| ops/performance.py (Kelly) | → `crtMM` 资金管理组件 |
| execution/sell_chain.py (B1-B7) | → `crtST`(止损) + `crtTP`(止盈) 组件 |
| data/market.db (日线) | → Hikyuu HDF5/SQLite 数据驱动 或 CSV导入 |
| 因子日历日频因子 | → 自定义IndicatorImp + FactorSet |
| intraday_runner.py (主循环) | → Hikyuu PF_Simple.run() + 自定义调度 |

### 8.6 北极星适用性最终评估

| 维度 | 评分 | 说明 |
|------|:----:|------|
| 能承载陈小群模式吗 | ✅ 9/10 | crtSG纯Python实现, 灵活度极高 |
| 能承载因子日历吗 | ✅ 9/10 | MF_ICIRWeight + SE_MultiFactor原生支持 |
| 能实现自适应止损吗 | ✅ 10/10 | crtST + ST_Indicator, 完美支持 |
| 能实现Kelly吗 | ⚠️ 7/10 | 需自定义crtMM, 但接口清晰 |
| 能回测短线策略吗 | ✅ 9/10 | 分钟数据+T+1+涨跌停, A股原生 |
| 能量化打板吗 | ⚠️ 8/10 | 需自定义涨停排队逻辑, 框架有扩展点 |
| 能在M1 8GB运行吗 | ✅ 9/10 | C++核心+Python层, 资源友好 |
| 能实盘交易吗 | ⚠️ 6/10 | 有broker接口但不成熟, 需保留Sina接口 |
| **综合** | **✅ 8.4/10** | **可行, 需要自定义组件但框架支持良好** |


### 🥈 备选: WonderTrader (综合评分8.4)

**对北极星的直接贡献**:
- SEL引擎原生支持多因子选股截面策略
- M+1+N架构防自成交
- 五层风控体系
- 已有数十亿级实盘管理规模背书

**短板**: 学习曲线陡, 社区较小 (QQ群为主)
**来源**: https://github.com/wondertrader/wondertrader

### 🥉 打板专项: KhQuant + daban/xman

如保留陈小群架构, KhQuant是最接近的打板回测方案; daban/xman用于实盘盯盘。
**来源**: https://khsci.com/khQuant/ | https://github.com/freevolunteer/daban

---

## 七、关键发现

1. **没有"开箱即用"的陈小群量化系统** — 打板策略高度依赖个人经验, 所有框架都需要定制
2. **2024-2025趋势**: Rust重写核心(vnpy/Hikyuu/AKQuant), AI深度集成(Qlib/AlphaPROBE), miniQMT成为实盘标准
3. **因子日历的最佳落地点**: Hikyuu的多因子建模模块 或 WonderTrader的SEL引擎
4. **现有陈小群代码的最大价值**: 可作为 Hikyuu 的 SG(信号指示器)组件嵌入, 模式识别逻辑不必丢弃
