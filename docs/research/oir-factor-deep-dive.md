# OIR 隔夜-日内反转因子深度解析

> 2026-07-07 | 基于华安证券(2020)原始报告及后续6家券商验证

---

## 零、命名澄清

"OIR"在文献中有两种指代：

| 含义 | 来源 | 说明 |
|------|------|------|
| **Overnight-Intraday Reversal** | 曲荣华/刘扬《经济学报》(2020) | 学术论文中的效应名称，泛指隔夜-日内反转现象 |
| **Order Imbalance Ratio** | 微观结构文献 | 订单不平衡比率 = (买量-卖量)/(买量+卖量)，是另一个独立因子 |

**华安证券(2020)的原始报告并未使用"OIR"这个缩写**，其核心因子称为"昼夜合成因子"。本文档以华安原始因子为核心，兼论后续改进版本。

---

## 一、基础定义：两种收益率的精确拆分

### 1.1 对数收益率拆分（华安证券采用）

日度对数收益率按开盘价精确拆分为隔夜+日内两部分：

$$\boxed{\ln\left(\frac{Close_t}{Close_{t-1}}\right) = \ln\left(\frac{Open_t}{Close_{t-1}}\right) + \ln\left(\frac{Close_t}{Open_t}\right)}$$

| 符号 | 名称 | 精确公式 | 含义 |
|------|------|---------|------|
| $r_t$ | 日度收益 | $\ln(Close_t / Close_{t-1})$ | t-1收盘 → t收盘 |
| $r_t^{night}$ | 隔夜收益 | $\ln(Open_t / Close_{t-1})$ | t-1收盘 → t开盘（集合竞价跳空） |
| $r_t^{intra}$ | 日内收益 | $\ln(Close_t / Open_t)$ | t开盘 → t收盘 |

> **为什么用对数收益率？** 对数收益率在时间序列上可加：$r_t = r_t^{night} + r_t^{intra}$。简单百分比收益率 $(Close_{t}/Close_{t-1}-1)$ 不可加，无法严格拆分。

### 1.2 月度累计（20个交易日）

华安证券采用月度调仓，每月末回溯过去20个交易日：

$$Ret_{night} = \sum_{t=1}^{20} \ln\left(\frac{Open_t}{Close_{t-1}}\right)$$

$$Ret_{intraday} = \sum_{t=1}^{20} \ln\left(\frac{Close_t}{Open_t}\right)$$

### 1.3 隔夜vs日内的方向差异（关键）

| 收益类型 | IC方向 | 效应类型 | IC均值（全A） |
|---------|--------|---------|-------------|
| $Ret_{night}$ | **正** | **动量** | +2.00% |
| $Ret_{intraday}$ | **负** | **反转** | -6.37% |
| $Ret_{close\\_to\\_close}$ | 负 | 反转（日内主导） | -5.56% |

**核心洞见**：传统反转因子把隔夜动量和日内反转混在一起，Alpha被稀释。隔夜部分（动量）抵消了日内部分（反转）的选股效果——**多头收益被抹平，只剩空头Alpha**。

---

## 二、华安证券(2020)原始因子

### 报告信息

| 项目 | 内容 |
|------|------|
| **标题** | 《市场微观结构剖析之九：昼夜分离，隔夜跳空与日内反转选股因子》 |
| **机构** | 华安证券 |
| **日期** | 2020年9月1日 |
| **分析师** | 朱定豪、严佳炜；研究助理：钱静闲 |
| **PDF** | [万得链接](https://bigdata-s3.wmcloud.com/researchreport/cc/c427d01b4f9897372ba3442f62f9b576.pdf) |
| **微信公众号** | [华安金工](http://mp.weixin.qq.com/s?__biz=MzI0NzMyNzQ2NQ==&mid=2247498994&idx=1&sn=1b3408407ff90de6026d2c6cd5f73b99) |
| **BigQuant复现** | [昼夜分离因子](https://bigquant.com/wiki/doc/0V7PUsN9g8) |

### 2.1 日内反转：黄金分割点

开盘前半小时（9:30-10:00）因流动性差、买卖价差大，收益近乎噪音。**10:00是黄金分割点**：

$$Ret_{10:00\\_to\\_close} = \sum_{t=1}^{20} \ln\left(\frac{Close_t}{Price_{10:00_t}}\right)$$

各时段的IC贡献（华安实测）：

| 时段 | IC均值 | Rank IC | 反转强度 |
|------|--------|---------|---------|
| 隔夜 | +2.00% | +3.00% | ✗（动量） |
| 9:30-10:00 | -0.41% | -1.42% | ≈噪音 |
| 10:00-10:30 | -1.33% | -1.65% | ✓ |
| 11:00-11:30 | -2.81% | -2.85% | ✓ |
| **13:00-13:30** | **-3.51%** | **-4.17%** | ✓✓（最强） |
| 14:30-15:00 | -3.37% | -2.57% | ✓ |

### 2.2 隔夜跳空因子

隔夜跳空引入**绝对值**处理。无论高开还是低开，次月均为负向Alpha（呈现抛物线型分组收益）：

$$Ret_{night\\_jump} = \left|\sum_{t=1}^{10} \ln\left(\frac{Open_t}{Close_{t-1}}\right)\right|$$

> **为何取绝对值？** 隔夜涨跌幅的绝对值（而非方向）是核心信号——大幅高开（过度乐观）和大幅低开（过度悲观）都会在次月反转。取10日窗口是因为隔夜跳空因子的IC衰减更快，"更健忘"。

### 2.3 昼夜合成因子（核心公式）

将日内黄金分割反转与隔夜跳空按 **6:4 权重**合成（参数扫描验证最优）：

$$\boxed{F_{day\\_night} = 0.6 \times Ret_{10:00\\_to\\_close} + 0.4 \times Ret_{night\\_jump}}$$

> **取负号（-）的原因**：$Ret_{10:00\\_to\\_close}$ 本身的IC为负（日内上涨→未来下跌），$Ret_{night\\_jump}$ 的IC也为负（跳空幅度大→未来下跌）。合成后整体IC为负。在实际使用中，因子值取负方向——**因子值越小（IC负向）→买入信号**。

### 2.4 回测表现

市值+行业中性化后（2010/01-2020/08，全A，月频调仓）：

| 指标 | 传统反转因子 | 昼夜合成因子 |
|------|-------------|-------------|
| IC均值 | -5.56% | **-8.10%** |
| RankIC均值 | -6.81% | **-9.66%** |
| 年化ICIR | — | **-4.04** |
| 多空年化收益 | — | **36.92%** |
| 年化波动 | — | 7.93% |
| 信息比率(IR) | 2.27 | **4.66** |
| 月度胜率 | 72% | **89.6%** |
| 最大回撤 | 12.94% | **7.58%** |
| 多头年化 | 7.19% | **13.29%** |
| 空头年化 | — | -23.63% |

---

## 三、学术来源：曲荣华/刘扬(2020)

### 论文信息

| 项目 | 内容 |
|------|------|
| **标题** | 《中国A股的隔夜-日内反转效应》/《多空对决与股票横截面收益》 |
| **期刊** | 《经济学报》(2020) |
| **链接** | [CNKI](https://tsjje.cbpt.cnki.net/WKG/WebPublication/paperDigest.aspx?paperID=629d4ef0-5e8f-486f-8519-bf2c72637247) |

### 核心发现

曲荣华/刘扬首次系统性地将A股日度收益拆分为隔夜和日内两部分，发现：

1. **隔夜收益为负、日内收益为正** —— 与全球主要市场（隔夜正溢价）完全相反
2. **T+1制度是根本原因** —— 买入者被锁定至次日，开盘价需补偿"日内不可卖出"的流动性风险
3. **隔夜-日内存在跨期反转** —— 当月日内收益高的股票，次月收益显著为负

> **注意**：曲荣华/刘扬的论文建立了"隔夜-日内反转"这一现象的理论框架，但并未给出华安证券那种可直接交易的合成因子公式。华安证券(2020)是首篇将该现象转化为可交易选股因子的券商研报。

---

## 四、后续验证与改进

### 4.1 西部证券 TOI（Tug of Overnight-Intraday）因子 (2024.11)

| 项目 | 内容 |
|------|------|
| **报告** | 《因子手工作坊系列(1)：隔夜上涨和日内反转中的隐藏ALPHA》 |
| **分析师** | 冯佳睿 |
| **链接** | [新浪财经](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=784402588943) |

**逻辑**：隔夜上涨但日内涨幅未维持的交易日 → 多空分歧 → 后续修复性上涨。

**精确构建步骤**：

1. 定义"拉锯日"：$\{t: r_t^{night} > 0 \text{ 且 } r_t^{intra} < r_t^{night}\}$
2. 在拉锯日子样本中，计算：
   $$TOI = \text{Corr}\left(r_t^{night} - r_t^{intra},\; \frac{Volume_t^{intra}}{Volume_t^{total}}\right)$$
3. 取月度截面秩相关系数（**Spearman**）

| 指标 | 数值 |
|------|------|
| IC均值 | **+0.035**（正向！） |
| 年化ICIR | **+2.75** |
| 月度胜率 | 83% |
| 多空年化 | 8.47% |

> **注意**：TOI的IC为**正**值，方向与华安昼夜因子相反。TOI捕捉的是"分歧后修复"，而非直接反转。

### 4.2 中信建投 32因子体系 (2025.11)

| 项目 | 内容 |
|------|------|
| **报告** | 《逐鹿Alpha专题报告(二十九)：隔夜-日内异象因子及领先滞后分析》 |
| **分析师** | 姚紫薇、王超、苏良 |
| **链接** | [新浪财经](https://finance.sina.com.cn/wm/2025-11-26/doc-infystcp1980904.shtml) |

**覆盖因子类别（9大类32子因子，全部N=20日窗口）**：

| 类别 | 代表因子 | 说明 |
|------|---------|------|
| 动量/反转 | `intraday_reversal_20` | 日内收益反转 |
| 强度/背离 | `oi_spread_20` | 隔夜-日内收益率差 |
| 波动/稳定性 | `overnight_volatility_20` | 隔夜波动率 |
| 量价相关性 | `oi_volume_correlation_20` | **表现最优** |
| 极值 | `overnight_max_return_20` | 隔夜最大收益 |
| 非对称性 | `oi_asymmetry_20` | 隔夜-日内不对称 |
| 持续性 | `overnight_persistence_20` | 隔夜持续性 |
| 相对强弱 | `intraday_pos_neg_ratio_20` | 日内正负收益比 |
| 信号强度 | `overnight_signal_strength_20` | 隔夜信号强度 |

**表现最好的3个**（多空夏普>3）：
- `oi_volume_correlation_20`：隔夜收益与日内成交量的20日相关性
- `oi_volume_ratio_20`：隔夜-日内成交量比率20日均值
- `overnight_volume_momentum_20`：隔夜成交量动量20日

**领先-滞后策略**（d-LE-SC算法）：
- 将股票分为"领导者"与"跟随者"，利用领导者隔夜表现预测跟随者日内走势
- 年化收益14.99%，Alpha=11.34%
- LightGBM+pegformer集成：IC=0.087，IR=9.136

### 4.3 东吴证券 RPV/SRV 价量相关性因子 (2022)

| 项目 | 内容 |
|------|------|
| **报告** | 《价量相关性RPV因子——日内与隔夜价量关系的融合》 |
| **链接** | [新浪财经](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/714124986210/index.phtml) |

**构建方法**：分别计算日内价量相关系数和隔夜价量相关系数，合成复合因子。

$$RPV = \text{Corr}(r^{intra}, Volume^{intra}) - \text{Corr}(r^{night}, Volume^{night})$$

| 指标 | 数值 |
|------|------|
| RankIC均值 | -0.0576 |
| RankICIR | -4.26 |
| 月度胜率 | 80% |

### 4.4 国盛证券 MIF 市场非有效性因子 (2022.04)

| 项目 | 内容 |
|------|------|
| **报告** | 《隔夜涨跌的新用法——市场非有效性因子MIF》 |
| **链接** | [新浪财经](https://finance.sina.cn/2022-06-29/detail-imizmscu9253305.d.html) |

$$MIF = \text{SpearmanCorr}\left(\left|r_t^{night}\right|,\; Turnover_{t-1}\right)$$

> 使用**Spearman**秩相关而非Pearson，避免极端值影响。

| 指标 | 数值 |
|------|------|
| 多空年化 | 10.91% |
| IR | **2.49** |
| 月度胜率 | 73.55% |
| 最大回撤 | 2.70% |
| 与昼夜合成因子相关性 | **仅0.035**（增量信息极强） |

---

## 五、"秩相关系数"版本的澄清

用户询问"两部分收益的秩相关系数"计算方式。在原始文献中：

### 5.1 华安昼夜合成因子：**不使用秩相关**

华安原始因子是 **6:4线性加权**，不是相关系数。计算步骤：
1. 每月末，对每只股票计算 $Ret_{10:00\\_to\\_close}$（20日累计对数收益）
2. 计算 $Ret_{night\\_jump}$（10日隔夜绝对值累计）
3. 线性合成 $F = 0.6 \times Ret_{10:00\\_to\\_close} + 0.4 \times Ret_{night\\_jump}$
4. 横截面标准化 → 因子值越小（越负）→ 买入

### 5.2 使用秩相关的版本：国盛MIF和东吴RPV

| 因子 | 秩相关对象 | 相关性类型 | 窗口 |
|------|-----------|-----------|------|
| **MIF** | |隔夜收益| vs 换手率 | **Spearman** | 月度截面 |
| **RPV** | 日内收益 vs 日内成交量 + 隔夜收益 vs 隔夜成交量 | **Pearson**（时序） | 月度滚动 |
| **TOI** | (隔夜-日内差) vs 日内成交量占比 | **Spearman**（截面） | 月度截面 |

**如果要用秩相关构建隔夜-日内反转因子**，最自然的做法是：

$$OIR_{rank} = -\text{SpearmanCorr}\left(\{r_{i}^{intra}\}_{i=1}^{N},\; \{r_{i}^{night}\}_{i=1}^{N}\right)_{\text{monthly cross-section}}$$

其中 $N$ 为当月截面股票数。$r_i^{intra}$ 和 $r_i^{night}$ 分别为个股 $i$ 的月度累计日内收益和隔夜收益。取负号是因为两者在截面上呈负相关（隔夜动量+日内反转方向相反），负相关越强→反转信号越强。

---

## 六、数据要求

### 6.1 基础版（华安昼夜合成因子）

| 数据字段 | 频率 | 说明 |
|---------|------|------|
| Open, Close | 日频 | 计算隔夜/日内收益 |
| 10:00价格 | 分钟级(仅需一个快照) | 黄金分割点反转 |
| 行业分类 | 申万一级 | 行业中性化 |
| 流通市值 | 日频 | 市值中性化 |

**akshare获取**：
- 日频OHLC：`akshare.stock_zh_a_hist()` → 免费
- 10:00价格：`akshare.stock_zh_a_hist_min_em()` 获取分钟K线取10:00 → 免费（量较大）
- 行业分类：`akshare.stock_board_industry_name_em()` → 免费

> **如果无法获取10:00价格**：用 `(Open+Close)/2` 近似替代？不推荐。应直接用标准日内反转 $Ret_{intraday}$（Open→Close），IC从-7.37%降至-6.37%，损失约1个百分点但完全可接受。

### 6.2 增强版

| 版本 | 额外数据 | 获取方式 |
|------|---------|---------|
| TOI（西部） | 日内成交量占比 | 需要分钟成交量 |
| MIF（国盛） | 换手率 | akshare日频免费 |
| 32因子体系（中信建投） | 分钟级量价 | tushare pro/聚宽付费 |
| 领先-滞后网络 | 全市场分钟数据 | Level-2付费 |

---

## 七、已知问题

### 7.1 IC衰减速度

| 因子成分 | 衰减特征 |
|---------|---------|
| **隔夜跳空因子** | 衰减**快**，月度调仓下**10日窗口最优**（vs 20日窗口的日内部分） |
| **日内反转因子** | 衰减**慢**，20日窗口仍有效 |
| **传统反转因子** | 半衰期约**1个月**（次月衰减一半以上） |
| **日内残差高阶矩** | 半衰期约**2周**（东方证券） |

**启示**：
- 隔夜跳空部分适合**周频调仓**捕捉短期Alpha
- 日内反转部分月频即可
- 提高调仓频率（从月频→周频）可改善IR，但需考虑交易成本

### 7.2 市场状态依赖

| 市场状态 | 因子表现 | 原因 |
|---------|---------|------|
| **震荡市** | 最优 | 均值回复逻辑有效 |
| **趋势牛市中后期** | 减弱 | 动量效应主导，反转因子被碾压 |
| **暴跌/股灾** | 空头端仍有效 | 高日内收益组仍然大跌 |
| **流动性危机**（如2024.02雪球/DMA） | 短期失效 | 微观结构破裂 |

华安全样本（2010-2020）覆盖2015牛、2016熔断、2018熊、2019-2020牛，因子最大回撤仅7.58%，整体稳健。

### 7.3 流动性偏差

| 偏差 | 方向和幅度 | 应对 |
|------|-----------|------|
| **小市值偏好** | 反转效应在小市值更强（多空1.12%/月 vs 大市值0.65%/月） | 市值中性化 |
| **低流动性偏好** | 高换手率股票的隔夜-日内偏离更大 | 剔除最低流动性20% |
| **涨停/跌停干扰** | 一字板无法交易，收益为0但被计入 | 剔除涨跌停日 |
| **开盘流动性不足** | 9:30-10:00买卖价差大，收益噪音化 | 从10:00起算（华安方案） |

### 7.4 换手率交互

- 高换手率股票：隔夜收益率**更低**（损失更大），日内收益率**更高**
- 日内收益+高换手 → **最强调反转**（ICIR可达-2.72，胜率80% vs 传统70.8%）
- 低换手率下的日内反转 → 弱动量而非反转

### 7.5 组合容量

海通证券研究：组合规模超过**30亿**后，换手率类因子IC显著下降。隔夜-日内反转因子的容量比纯换手率因子大（因为逻辑基础是T+1制度而非拥挤交易），但超过50亿后仍需关注衰减。

---

## 八、改进版本汇总

| 改进方向 | 方法 | 效果 | 来源 |
|---------|------|------|------|
| **行业中性化** | 对申万一级行业哑变量回归取残差 | ICIR从-3.5→-4.04 | 华安(2020) |
| **强中性化** | +市值+波动+换手全部剥离 | IC从-8.1%→-5.49%（更纯净） | 华安(2020) |
| **换手率加权** | 日内收益×换手率偏差 | ICIR提升至-2.72 | 东吴(2022) |
| **成交量加权RSI** | 1分钟RSI×换手率 | ICIR=-2.34, 多空25.89% | 国盛(2023) |
| **TOI拉锯因子** | 仅取隔夜涨+日内弱的子样本 | ICIR=+2.75（正向，方向独立） | 西部(2024) |
| **MIF正交叠加** | 昼夜因子+MIF（相关性0.035） | 增量IR叠加 | 国盛(2022) |
| **32因子集成** | d-LE-SC聚类+LightGBM | IC=8.7%, IR=9.14 | 中信建投(2025) |
| **周频调仓** | 月频→周频 | TRCF的ICIR从4.19→6.56 | 开源(2024) |

---

## 九、推荐实现方案

### Python伪代码（akshare基础版）

```python
import akshare as ak
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

def compute_day_night_factor(df: pd.DataFrame, 
                              night_window: int = 10,
                              intraday_window: int = 20,
                              weight_night: float = 0.4,
                              weight_intra: float = 0.6):
    """
    Compute 华安昼夜合成因子 from daily OHLC data.
    
    Args:
        df: DataFrame with columns [date, open, close, high, low, volume, 
             industry, market_cap] per stock
        night_window: rolling window for overnight jump (default 10)
        intraday_window: rolling window for intraday reversal (default 20)
        weight_night: weight for overnight jump component
        weight_intra: weight for intraday reversal component
    
    Returns:
        Series of factor values per stock (lower = stronger buy signal)
    """
    # Step 1: Compute log returns
    df['ret_night'] = np.log(df['open'] / df['close'].shift(1))  # overnight
    df['ret_intra'] = np.log(df['close'] / df['open'])            # intraday
    
    # Step 2: Rolling cumulative sums
    # Intraday reversal: sum of log intraday returns over intraday_window
    df['ret_intra_cum'] = df.groupby('code')['ret_intra'].rolling(
        intraday_window).sum().reset_index(level=0, drop=True)
    
    # Overnight jump: absolute sum over night_window (shorter window!)
    df['ret_night_jump'] = df.groupby('code')['ret_night'].rolling(
        night_window).apply(lambda x: np.abs(x).sum()).reset_index(level=0, drop=True)
    
    # Step 3: Composite factor (before neutralization)
    df['raw_factor'] = (weight_intra * df['ret_intra_cum'] + 
                         weight_night * df['ret_night_jump'])
    
    # Step 4: Industry + market cap neutralization (cross-sectional each month)
    df = df.dropna()
    for month, group in df.groupby('month'):
        # OLS: raw_factor ~ industry_dummies + log(market_cap)
        X = pd.get_dummies(group['industry']).astype(float)
        X['log_mcap'] = np.log(group['market_cap'])
        from sklearn.linear_model import LinearRegression
        resid = group['raw_factor'] - LinearRegression().fit(
            X, group['raw_factor']).predict(X)
        df.loc[resid.index, 'factor_neutral'] = resid
    
    # Step 5: Z-score cross-sectionally
    df['factor_z'] = df.groupby('month')['factor_neutral'].transform(
        lambda x: (x - x.mean()) / x.std())
    
    return df['factor_z']  # NEGATIVE = buy signal

# Alternat​​ive: MIF-style rank correlation version
def compute_OIR_rank_correlation(df, window=20):
    """
    Rank correlation version: SpearmanCorr(intraday_returns, overnight_returns)
    across the cross-section each month, negated.
    """
    monthly_factors = {}
    for month, group in df.groupby('month'):
        intraday_ret = group['ret_intra'].sum()   # monthly cumulative
        overnight_ret = group['ret_night'].sum()
        # Spearman rank correlation across stocks
        rho, _ = spearmanr(intraday_ret, overnight_ret)
        # Negative: stronger negative correlation = stronger reversal signal
        monthly_factors[month] = -rho
    return pd.Series(monthly_factors)
```

### 参数推荐值

| 参数 | 推荐值 | 来源 |
|------|--------|------|
| 日内窗口 | **20日**（月频）或 **10日**（双周频） | 华安(2020) |
| 隔夜跳空窗口 | **10日**（比日内短） | 华安(2020) |
| 权重(日内:隔夜) | **6:4** | 华安参数扫描 |
| 中性化 | **行业+市值**（基准版） | 所有券商共识 |
| 去极值 | **MAD 5倍** | 标准流程 |
| 调仓频率 | **月频**（基准）/ **周频**（增强） | 视交易成本 |
| 股票池 | **全A剔除ST/停牌/上市<60天/一字板** | 标准流程 |

---

## 十、关键来源

1. **华安证券**《市场微观结构剖析之九：昼夜分离，隔夜跳空与日内反转选股因子》(2020.09) — [万得PDF](https://bigdata-s3.wmcloud.com/researchreport/cc/c427d01b4f9897372ba3442f62f9b576.pdf) | [BigQuant复现](https://bigquant.com/wiki/doc/0V7PUsN9g8)
2. **曲荣华/刘扬**《中国A股的隔夜-日内反转效应》(2020) — [经济学报](https://tsjje.cbpt.cnki.net/WKG/WebPublication/paperDigest.aspx?paperID=629d4ef0-5e8f-486f-8519-bf2c72637247)
3. **西部证券**《因子手工作坊(1)：隔夜上涨和日内反转中的隐藏ALPHA》(2024.11) — [新浪财经](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=784402588943)
4. **中信建投**《逐鹿Alpha(二十九)：隔夜-日内异象因子及领先滞后分析》(2025.11) — [新浪财经](https://finance.sina.com.cn/wm/2025-11-26/doc-infystcp1980904.shtml)
5. **东吴证券**《价量相关性RPV因子——日内与隔夜价量关系的融合》(2022.10)
6. **国盛证券**《隔夜涨跌的新用法——市场非有效性因子MIF》(2022.04) — [新浪财经](https://finance.sina.cn/2022-06-29/detail-imizmscu9253305.d.html)
7. **东方证券**《日内残差高阶矩与股票收益》(2016.08) — IC半衰期约2周
