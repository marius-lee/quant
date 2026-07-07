# 异常换手率残差因子深度解析

> 2026-07-07 | 多来源综合：中银证券、国信证券、东方证券、学术论文

---

## 零、概念澄清：两个易混淆因子

"残差换手率"在文献中指代两个**完全不同**的因子，必须区分：

| | 异常换手率残差（本文主题） | 特质换手波动率 |
|---|---|---|
| **英文** | Abnormal/Residual Turnover Level | Idiosyncratic Turnover Volatility |
| **回归什么** | **截面回归**：每月末，个股换手率 ~ 市值+行业 → 取残差 | **时序回归**：每只股票，换手率 ~ FF换手因子 → 取残差的 std |
| **度量什么** | 换手率的**异常水平**（高/低于预期） | 换手率剥离风格后的**波动幅度** |
| **类比** | 异常收益率（CAR） | 特质波动率（IVol） |
| **方向** | 负（异常高换手→未来低收益） | 负（高特质换手波动→未来低收益） |

本文聚焦第一种——**截面回归残差作为异常换手率水平因子**。

---

## 一、精确回归模型

### 1.1 基础模型：市值中性化

每月末，在全市场截面上做 OLS 回归：

$$\boxed{\ln(Turnover_{i,t}) = \alpha_t + \beta_t \cdot \ln(MktCap_{i,t}) + \varepsilon_{i,t}}$$

| 符号 | 含义 |
|------|------|
| $\ln(Turnover_{i,t})$ | 股票 $i$ 在第 $t$ 月末的**过去20个交易日日均换手率**的自然对数 |
| $\ln(MktCap_{i,t})$ | 股票 $i$ 在第 $t$ 月末的**流通市值**的自然对数 |
| $\beta_t$ | 市值敏感系数，通常为**负**（大市值→低换手） |
| $\varepsilon_{i,t}$ | **残差 = 异常换手率原始值** |

### 1.2 标准模型：市值 + 行业双中性化

$$\boxed{\ln(Turnover_{i,t}) = \alpha_t + \beta_t \cdot \ln(MktCap_{i,t}) + \sum_{k=1}^{K} \gamma_{k,t} \cdot \mathbf{1}[Ind_i = k] + \varepsilon_{i,t}}$$

其中 $\mathbf{1}[Ind_i = k]$ 为行业哑变量（申万一级，28-31个行业）。

### 1.3 扩展模型（可选，IC 提升有限但更纯净）

部分券商（财通证券"拾穗"系列）进一步加入：

$$\ln(Turnover_{i,t}) = \alpha_t + \beta_{1,t} \cdot \ln(MktCap_{i,t}) + \beta_{2,t} \cdot [\ln(MktCap_{i,t})]^2 + \sum_{k} \gamma_{k,t} \cdot \mathbf{1}[Ind_i = k] + \varepsilon_{i,t}$$

> 加入市值平方项可将 $R^2$ 从约 15.5% 提升至 20.0%（财通证券实证），但对因子 IC 的提升约 0.3-0.5 个百分点。

### 1.4 为什么取对数？

换手率横截面分布严重右偏（少数股票换手率极高）。取对数后分布接近正态，OLS 残差更有意义。

### 1.5 回归形式：逐月截面回归，非面板回归

| 选择 | 理由 |
|------|------|
| **逐月截面 OLS**（每月单独跑一次回归） | ✅ **标准做法** |
| 全样本面板回归 | ❌ 月度间截距和系数变化显著，面板假设不成立 |
| Fama-MacBeth 两步法 | ⚠️ 可用于显著性检验，但不用于因子构造 |

---

## 二、因子值的符号约定

### 2.1 残差的原始含义

$$\varepsilon_{i,t} = \ln(Turnover_{i,t}) - \widehat{\ln(Turnover_{i,t})}$$

| $\varepsilon > 0$ | 换手率**高于**市值+行业预期 → 异常活跃 → 🔴 看空 |
| $\varepsilon < 0$ | 换手率**低于**市值+行业预期 → 异常冷淡 → 🟢 看多 |
| $\varepsilon \approx 0$ | 换手率符合预期 |

### 2.2 两种因子方向（等效，选取一即可）

**方案A（推荐，与券商研报一致）**：取**负残差**作为因子值，使因子方向为正（因子值越大=越好的股票）

$$\boxed{ABN\_TURN = -\varepsilon_{i,t}}$$

- ABN_TURN > 0 → 换手率异常偏低 → 买入信号
- ABN_TURN < 0 → 换手率异常偏高 → 卖出信号（投机过热，后续反转）

**方案B（等效）**：直接用原始残差，但在组合构建时取负向排序。

> 两种方案数学上等价，方案A更符合"因子值越大越看多"的直觉。

---

## 三、出处

### 3.1 学术来源

| 文献 | 贡献 |
|------|------|
| **Chordia, Subrahmanyam & Anshuman (2001)**, *JFE* "Trading Activity and Expected Stock Returns" | 首次发现换手率**波动率**（std turnover）与未来收益的强负相关，奠定了"异常交易活动"的研究基础 |
| **Chordia, Huh & Subrahmanyam (2007)**, *JFE* "The Cross-Section of Expected Trading Activity" | 提出用**残差法**度量异常交易量：对换手率做市值+行业回归，残差即为异常交投 |

### 3.2 A股实证来源

| 来源 | 贡献 |
|------|------|
| **东方证券 朱剑涛 (2015)**《投机、交易行为与股票收益(上)》 | 最早将残差换手率引入A股选股 |
| **中银证券 (2018-2024)** 多因子框架 | 在价值/质量因子组合中使用残差换手率做辅助筛选 |
| **国信证券 (2022)** 隐式因子框架 | 特质换手波动率（本因子的波动率版本），RankIC=-5.60%，ICIR=-4.21 |
| **财通证券 (2023-2024)** "拾穗"系列 | 对比回归法vs分组法，提出非线性改进 |
| **《引入换手率相关指标的多因子模型——A股实证分析》**（学术论文） | Fama-MacBeth回归实证，IC=-6.77%，IR=-0.57（所有测试因子中最优） |

---

## 四、实证数据

### 4.1 IC/IR（学术论文，Fama-MacBeth回归，全A）

| 因子 | Rank IC 均值 | Rank IC 标准差 | IR绝对值 | t值 |
|------|:---:|:---:|:---:|:---:|
| 传统换手率 | -7.70% | 0.15 | 0.52 | — |
| **异常换手率残差** | **-6.77%** | 0.12 | **0.57** | **-8.68** |
| 规模（ln市值） | -2.94% | 0.20 | 0.15 | — |
| 价值（BM） | 5.23% | 0.15 | 0.35 | — |
| 盈利（ROE） | 3.60% | 0.18 | 0.20 | — |

> 异常换手率残差的 IR=0.57 为**所有测试因子中最高**，说明其预测稳定性在所有常见风格因子中最优。

### 4.2 经济意义

- 残差每增加 1 个标准差 → 下月收益降低约 **0.70%**（月度）
- 最低残差组（换手率异常冷淡）长期累计收益 **11.33倍**（2000-2020）
- 剔除最小市值30%后，年化仍有 **9.37%**

### 4.3 国信证券版本（特质换手波动，2022）

| 指标 | 数值 |
|------|:---:|
| RankIC均值 | **-5.60%** |
| 年化ICIR | **-4.21** |
| 月度胜率 | **90%** |
| 多空月均超额 | 1.36% |

---

## 五、与相关因子的区别

### 5.1 vs 普通换手率反转（turnover_rev）

| | 普通换手率反转 | 异常换手率残差 |
|---|---|---|
| **计算** | $-\text{mean}(Turnover_{1..20})$ | $-\varepsilon$ from turnover ~ mcap + industry |
| **选出的股票** | 绝对低换手率股 | 相对其市值/行业"异常低换手"的股 |
| **问题** | 永远选小盘股、冷门行业 | 大盘股也可能被选中（如果相对同市值异常安静） |
| **行业偏离** | 严重（天然偏金融/公用事业等低换手行业） | 已中性化，无行业偏离 |
| **IC 提升** | — | IR 从 0.52 → 0.57（约+10%） |

### 5.2 vs 东吴 STR（量稳换手率）

| | STR（东吴） | ABN_TURN（本因子） |
|---|---|---|
| **公式** | $-\sigma(Turnover_{1..20})$ | $-\varepsilon$ from $\ln(Turn) \sim \ln(MktCap)$ |
| **度量** | 换手率的**稳定性**（波动幅度） | 换手率的**异常水平**（偏离预期） |
| **捕捉** | "量稳" | "量异常" |
| **相关性** | 0.86（STR ↔ Turn20），与ABN_TURN约0.3-0.5 | — |
| **互补性** | 分离"稳"与"不稳" | 分离"预期内"与"异常" |

> STR 问的是："你的换手率波动大吗？"（稳定性）
> ABN_TURN 问的是："你的换手率比你该有的水平高吗？"（异常性）
> 两者并不冲突——一只股票可能换手率稳定（STR高分）但始终高于同市值预期（ABN_TURN低分），反之亦然。

### 5.3 vs Lee & Swaminathan (2000) Turnover Reversal

| | Lee & Swaminathan (2000) | 本因子（ABN_TURN） |
|---|---|---|
| **市场** | 美股 | A股 |
| **方法** | 直接用换手率水平做 double-sort（换手率×过去收益） | 先回归取残差再排序 |
| **核心发现** | 高换手率股像"魅力股"，低换手率股像"价值股" | 加入市值/行业控制后，残差比原始换手率 IC 更稳定 |
| **A股适用性** | 原始 L&S 方法在A股效果偏弱（动量部分IC≈0） | 残差法更适合A股（板板制度+行业分化大） |

---

## 六、数据要求

### 仅需日频数据，akshare 完全可满足

| 数据 | 频率 | 获取 |
|------|------|------|
| 日换手率 | 日频 | `akshare.stock_zh_a_hist()` → `turnover_rate` 列 |
| 流通市值 | 日频 | 同上 → `market_cap` 列 |
| 申万一级行业 | 静态 | `akshare.stock_board_industry_name_em()` → 免费 |

---

## 七、已知坑位

### 7.1 换手率分布的极端值

换手率在A股的分布极度右偏——微盘股日换手率可达20-30%，大盘股仅0.1-0.5%。即使取对数后仍有厚尾。

**应对**：
- 做回归前先对换手率做 **MAD 5倍截尾**
- 剔除日换手率 > 15% 的极端异常日（新股/炒作股）
- 或使用 **Winsorize**（1%/99% 分位数截尾）

### 7.2 行业分类粒度

| 粒度 | 哑变量数量 | 优点 | 缺点 |
|------|:---:|------|------|
| **申万一级** (28-31个) | 27-30 | 标准做法、不过拟合 | 行业内仍有异质性 |
| 申万二级 (~100个) | ~99 | R²更高、更纯净 | 过拟合、部分行业股票太少（残差不稳定） |
| 中信一级 | ~30 | 类似申万一级 | 数据库兼容性 |

> **推荐**：申万一级（28-31个哑变量）。二级行业在样本量不足时会造成回归矩阵秩亏和残差估计不稳定。

### 7.3 样本筛选

每月做截面回归前，需剔除：
- ST / *ST 股票
- 上市不满 **60个交易日** 的新股（换手率极高且不稳定）
- 当月停牌超过 **10个交易日** 的股票（日均换手率失真）
- 一字板涨停/跌停日（无法交易，换手率不反映真实交投）

### 7.4 市值平方项的必要性？

| 是否加平方项 | R² | IC提升 | 复杂度 |
|:---|:---:|:---:|:---:|
| 不加 | ~15.5% | 基准 | 低 |
| 加 $\ln(MktCap)^2$ | ~20.0% | +0.3~0.5pp | 略增 |

> **推荐**：对量化新手，从基础模型（市值+行业）开始。平方项是锦上添花，不是必需的。

### 7.5 状态依赖性（王少平教授研究）

异常换手率残差因子存在显著的**市场状态依赖**：

| 市场状态 | 因子表现 |
|---------|---------|
| **随机游走期**（正常市场） | ✅ 显著有效 |
| **泡沫期** | ❌ 不显著 |
| **恐慌/暴跌期** | 空头端仍有效，多头端减弱 |

> 2024年2月雪球/DMA踩踏和9.24政策行情期间，异常换手率类因子出现短期失效（与所有换手率因子一致）。

### 7.6 和 STR 的高相关性陷阱

异常换手率残差与 STR 的相关性约 0.3-0.5（中等），但两者都与 Turn20 高度相关（STR ↔ Turn20 = 0.86，ABN_TURN ↔ Turn20 ≈ 0.6-0.7）。如果系统中同时使用这三个因子，**必须做正交化**——建议以 Turn20 为基准，将 STR 和 ABN_TURN 分别对 Turn20 正交化取残差。

---

## 八、推荐实现

```python
import akshare as ak
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

def compute_ABN_TURN_factor(df: pd.DataFrame, window: int = 20):
    """
    Compute Abnormal Turnover Residual factor.
    
    Args:
        df: DataFrame with [date, code, turnover, market_cap, industry]
        window: trading days for average turnover
    
    Returns:
        DataFrame with monthly factor values (higher = better)
    """
    # Step 1: Monthly avg turnover (log)
    df = df.sort_values(['code', 'date'])
    df['turn_avg'] = df.groupby('code')['turnover'].transform(
        lambda x: x.rolling(window, min_periods=10).mean()
    )
    df['turn_avg'] = df['turn_avg'].clip(lower=0.01)  # avoid log(0)
    df['ln_turn'] = np.log(df['turn_avg'])
    df['ln_mcap'] = np.log(df['market_cap'])
    
    # Step 2: Extreme value trimming (MAD 5x on turnover)
    median = df['turn_avg'].median()
    mad = (df['turn_avg'] - median).abs().median()
    df = df[(df['turn_avg'] >= median - 5*mad) & 
            (df['turn_avg'] <= median + 5*mad)]
    
    # Step 3: Monthly cross-sectional regression
    monthly = df.groupby(['code', df['date'].dt.to_period('M')]).last().reset_index()
    
    industry_dummies = pd.get_dummies(monthly['industry'], prefix='ind')
    
    residuals = []
    for month, idx in monthly.groupby('date').groups.items():
        group = monthly.loc[idx]
        y = group['ln_turn'].values
        X = group[['ln_mcap']].values
        X = np.column_stack([X, industry_dummies.loc[idx].values])
        # Add intercept
        X = np.column_stack([np.ones(len(X)), X])
        
        model = LinearRegression().fit(X, y)
        resid = y - model.predict(X)
        
        temp = pd.DataFrame({'code': group['code'].values, 
                            'residual': resid}, index=idx)
        residuals.append(temp)
    
    monthly['residual'] = pd.concat(residuals)['residual']
    
    # Step 4: Cross-sectional z-score + NEGATE
    monthly['ABN_TURN'] = -monthly.groupby('date')['residual'].transform(
        lambda x: (x - x.mean()) / x.std()
    )
    
    return monthly[['date', 'code', 'ABN_TURN']]
    # ABN_TURN > 0 = abnormally LOW turnover = BUY signal
```

### 参数推荐

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| 换手率窗口 | **20日** | 匹配月频调仓 |
| 市值定义 | **流通市值** | 非总市值 |
| 行业分类 | **申万一级** (28-31类) | 不要用二级 |
| 去极值 | **MAD 5倍** | 在取对数前做 |
| 调仓频率 | **月频** | 标准 |
| 方向 | **取负残差** (负号) | ABN_TURN > 0 = 买入 |

---

## 九、关键来源

1. **Chordia, Huh & Subrahmanyam (2007)**, *JFE* "The Cross-Section of Expected Trading Activity" — 首次提出残差法度量异常交易量
2. **Chordia, Subrahmanyam & Anshuman (2001)**, *JFE* "Trading Activity and Expected Stock Returns" — 换手率波动与预期收益负相关
3. **东方证券 朱剑涛 (2015)**《投机、交易行为与股票收益(上)》— 将残差换手率引入A股
4. **国信证券 (2022)**《隐式框架下的特质类因子改进》— 特质换手波动率，RankIC=-5.60%，ICIR=-4.21
5. **财通证券 (2023-2024)**"拾穗"系列 — 回归法vs分组法对比，非线性改进
6. **《引入换手率相关指标的多因子模型——A股实证分析》** — Fama-MacBeth实证，异常换手率IC=-6.77%，IR=-0.57（最优）
7. **factors.directory** — [市值中性化换手率残差](https://factors.directory/zh/factors/liquidity/market-cap-adjusted-turnover) 标准化定义
