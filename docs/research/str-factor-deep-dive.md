# STR 量稳换手率因子深度解析

> 2026-07-07 | 东吴证券"技术分析拥抱选股因子"系列(七)~(十五)，2021-2025
> 研究员：高子剑、沈芷琦（首发）、凌志杰（绩效月报）

---

## 一、精确公式

### 1.1 原始 STR 因子（系列七，2021.05.15）

**每月末，对每只股票，计算过去20个交易日日换手率的标准差，取负值：**

$$\boxed{STR_{raw} = -\sigma(Turn_1, Turn_2, \ldots, Turn_{20})}$$

其中 $Turn_i$ = 第 $i$ 个交易日的当日换手率（单位：% 或小数，结果顺序不变）。

### 1.2 预处理与中性化

```
Step 1: 去极值 — MAD (Median Absolute Deviation) 5倍截断
Step 2: 缺失值填充 — 行业均值
Step 3: 截面标准化 — Z-score: (x - μ_cross) / σ_cross
Step 4: 市值中性化 — 对 log(流通市值) 回归取残差
Step 5: 再次 Z-score 标准化
```

$$\boxed{STR = -\text{Zscore}\left(\text{Neutralize}_{mcap}\left(\text{Zscore}\left(\sigma_{20d}(Turn)\right)\right)\right)}$$

### 1.3 是标准差，不是变异系数

| 指标 | STR 使用 |
|------|:------:|
| $\sigma(Turn_{1..20})$ — 标准差 | ✅ |
| $\sigma(Turn_{1..20}) / \mu(Turn_{1..20})$ — 变异系数 | ❌ |

**为什么不用变异系数？** 低换手率股票分母小 → 变异系数反而大 → 与"量稳"逻辑相悖。东吴用纯标准差，考察换手率的绝对波动幅度。

### 1.4 取负号的原因

换手率波动越大 → 未来收益越低（反转逻辑）。STR因子方向为**负**：
- **STR 值越小（越负）= 换手率波动越大 → 卖出信号**
- **STR 值越大（越接近0或正）= 换手率越稳定 → 买入信号**

这继承了换手率因子的通用逻辑：高波动/高换手 → 反转下跌。

### 1.5 与传统 Turn20 的区别

| 维度 | Turn20（量小因子） | STR（量稳因子） |
|------|-------------------|----------------|
| 度量对象 | 换手率**水平** | 换手率**稳定性** |
| 精确公式 | $\mu(Turn_{1..20})$ | $-\sigma(Turn_{1..20})$ |
| 方向 | 负（低换手→高收益） | 负（稳定换手→高收益） |
| 核心理念 | "量小" | "量稳" |
| 缺陷 | 高换手组内收益**分化严重**，误杀大涨股 | 区分"稳定低换手"vs"剧烈波动换手" |

**核心改进**：传统 Turn20 把"稳定低换手"和"某天突然放量后回落"的股票混在一起，两者水平相同但后续走势截然不同。STR 通过考察波动幅度将其分离。

---

## 二、出处

### 2.1 首发报告

| 项目 | 内容 |
|------|------|
| **标题** | 《量稳换手率选股因子——量小、量缩，都不如量稳？》 |
| **系列** | "技术分析拥抱选股因子"系列研究（七） |
| **日期** | **2021年5月15日** |
| **机构** | 东吴证券研究所 |
| **分析师** | **高子剑、沈芷琦** |
| **链接** | [新浪财经](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=674533759656) |

### 2.2 核心理念（原话）

> "量小、量缩，都不如量稳"

东吴通过统计分析 Turn20 十分组的组内标准差，发现高换手组内部收益的标准差远大于低换手组——说明"量小"只抓住了均值差异，忽视了组内分化。"量稳"正是为了捕捉这种分化。

### 2.3 全系列时间线

| 编号 | 报告 | 日期 | 核心贡献 |
|------|------|------|---------|
| 系列(七) | **量稳换手率 STR** | 2021.05.15 | 🥇 **STR 首发** |
| 系列(八) | 优加换手率 UTR | 2021.08.20 | STR + Turn20 优加法合成 |
| 系列(九) | 改进 STR（SCR） | 2021.12.07 | 双维度：横截面稳+时序稳 |
| 系列(十二) | UTR 2.0 | 2023.05.05 | 优加法2.0 |
| 系列(十五) | CTR 换手率切割刀 | 2024.01 | 隔夜收益切割换手率 |
| 绩效月报 | STR/SCR/GTR/TPS/SPS | 2023-至今 | 持续跟踪 |

---

## 三、实证数据

### 3.1 首发回测（2006/01-2021/04，全A，月频调仓，市值中性化）

| 指标 | Turn20 | **STR** | 提升 |
|------|:------:|:------:|:----:|
| IC 均值 | -0.072 | **-0.079** | +9.7% |
| 年化 ICIR | -2.10 | **-2.72** | +29.5% |
| 多空年化收益 | 33.41% | **42.99%** | +28.7% |
| 年化波动 | 17.35% | 14.42% | -16.9% |
| 信息比率(IR) | 1.90 | **2.96** | +55.8% |
| 月度胜率 | 71.58% | **77.60%** | +8.4% |
| 最大回撤 | 15.53% | **10.05%** | -35.3% |

### 3.2 最新绩效（截至2025/07，全A）

| 指标 | STR | Turn20 |
|------|:---:|:------:|
| 年化收益 | **40.75%** | ~35% |
| 年化波动 | 14.44% | ~17% |
| 信息比率 | **2.82** | ~2.0 |
| 月度胜率 | **77.02%** | ~72% |
| 最大回撤 | **9.96%** | ~16% |

### 3.3 2025年10月单月表现

2025年10月 STR 因子单月多空收益 **6.06%**，高于全年平均水平。

### 3.4 SPS 增强版（STR 纯净化+影线差，截至2025/11）

| 指标 | STR | **SPS** | 提升 |
|------|:---:|:------:|:----:|
| 年化收益 | 42.65% | **42.98%** | — |
| 年化波动 | 14.42% | 13.17% | -8.7% |
| 信息比率 | 2.96 | **3.27** | +10.5% |
| 月度胜率 | 76.21% | **83.54%** | +9.6% |

---

## 四、参数敏感性

### 4.1 窗口敏感性（东吴研报附录第12页）

| 窗口 | STR vs Turn20 | 结论 |
|------|:------------:|------|
| **10日** | STR 仍显著优于 Turn20 | IC≈-7.5%（CSDN复现） |
| **20日（默认）** | IC=-7.9%, ICIR=-2.72 | 基准参数 |
| **40日** | 多空对冲净值仍优于 Turn（研报图7） | 稳健 |
| **60日** | 多空对冲净值仍优于 Turn（研报图8） | 稳健 |

**结论**：STR 因子对回看窗口**不敏感**。10/20/40/60日窗口下，STR 均优于同窗口的传统 Turn 因子。20日窗口是东吴的默认选择（与月频调仓匹配），但窗口参数的选择空间很大。

### 4.2 为什么 STR 对参数不敏感？

传统 Turn 因子受窗口影响大（短窗口→噪音大，长窗口→信号钝化），但 STR 考察的是**稳定性**而非水平——无论窗口长短，波动剧烈 vs 波动平缓的区分都是稳定的。这是 STR 的根本优势。

---

## 五、相关性矩阵

### 5.1 换手率因子家族相关系数

| | Turn20 | STR | UTR | GTR | SPS | CTR |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Turn20** | 1.00 | **0.86** | 高 | **≤0.10** | — | — |
| **STR** | 0.86 | 1.00 | 高 | **≤0.10** | — | — |
| **UTR** | 高 | 高 | 1.00 | — | — | — |
| **GTR** | ≤0.10 | ≤0.10 | — | 1.00 | — | — |
| **SPS** | — | — | — | — | 1.00 | — |

> — 表示研报未披露该对相关系数

**关键发现**：
- **STR ↔ Turn20 ≈ 0.86**：高度相关！如果做多因子合成，必须先把 STR 对 Turn20 正交化（得 STR_deTurn20），否则1+1<2
- **GTR ↔ 所有因子 ≤ 0.10**：GTR（换手率变化率的稳定性）是唯一与所有其他换手率因子几乎正交的因子，叠加后效果显著

### 5.2 与 Barra 风格因子的相关性（以 UTR 为例）

| Barra 风格因子 | UTR 1.0 | UTR 2.0 |
|:---|---:|---:|
| Beta | 0.058 | -0.009 |
| BooktoPrice | -0.162 | -0.117 |
| **Liquidity** | **0.395** | **0.261** |
| **ResidualVolatility** | **0.373** | **0.259** |
| Size | 0.146 | 0.021 |
| Momentum | 0.166 | 0.086 |
| 其他 | <0.1 | <0.1 |

STR 类因子与流动性(Liquidity)和残差波动率(ResidualVolatility)有中等正相关（0.26~0.39），但与其他风格因子相关性低。这是合理的——换手率稳定→低流动性风险→低残差波动。

---

## 六、改进版本全景

### 6.1 SCR（系列九，2021.12.07）—— "要比别人稳，也要比自己稳"

**报告**：《改进STR：换手率要比别人稳 也要比自己稳》

**双维度稳定性**：

| 维度 | 含义 | 度量 |
|------|------|------|
| **横截面稳**（比别人稳） | 同期截面中，该股票的换手率比别的股票稳定 | 原始 STR |
| **时序稳**（比自己稳） | 该股票自身的换手率稳定性是否在恶化 | SCR = STR 的变化率 |

$$SCR = \frac{STR_t - STR_{t-1}}{|STR_{t-1}|}$$

即 SCR 衡量的是"量稳的变化率"——一个股票可能原来很稳但最近开始剧烈波动（时序不稳），这也是负面信号。

**绩效**（截至2025）：
| 指标 | STR | SCR |
|------|:---:|:---:|
| 多空年化 | 42.65% | ~20% |
| IR | 2.96 | ~1.5 |
| 与 STR 相关性 | — | 中等 |

### 6.2 UTR（系列八，2021.08.20）—— 优加换手率 1.0

**问题**：STR 和 Turn20 相关性 0.86，直接等权合成会1+1<2

**优加法**：
1. STR 排序 → 取前50%（稳的）+ 后50%（不稳的）
2. 在**前50%**（稳的）中，按 Turn20 排序 → **量小的在前**（低换手=好）
3. 在**后50%**（不稳的）中，按 Turn20 排序 → **量大的在前**（高换手+不稳=最差）

**绩效**：
| 指标 | STR | UTR |
|------|:---:|:---:|
| 年化收益 | 42.65% | 38.43% |
| 波动 | 14.42% | **12.52%** |
| IR | 2.96 | **3.07** |
| 胜率 | 76.21% | **79.13%** |
| 最大回撤 | 10.05% | **8.77%** |

> UTR 收益略低于 STR，但波动和回撤大幅降低，IR 反而更高——适合稳健型组合。

### 6.3 UTR 2.0（系列十二，2023.05.05）

将优加法从两段式改为连续打分+等权合成：
- 不再用 50% 硬切，而是连续排序打分
- IR 进一步提升至 **3.21**，胜率 **82.04%**

### 6.4 SPS（2023-2024）—— 纯净优加 + 影线差

**核心改进**：引入价格因子（影线差 PLUS）做价量配合

1. STR 对 PLUS 做横截面正交化 → STR_dePLUS（残差，波动显著降低50%）
2. PLUS 对 STR 做正交化 → PLUS_deSTR
3. 两残差标准化+非负化后相乘 → SPS

**绩效**：
| 指标 | STR | **SPS** |
|------|:---:|:------:|
| IR | 2.96 | **3.27** |
| 胜率 | 76.21% | **83.54%** |
| 最大回撤 | 10.05% | 11.58% |

### 6.5 SPS_Turbo（叠加 GTR）

将 SPS 再叠加 GTR（换手率变化率的稳定性，相关性≤0.10）：
- IR 达 **3.91**，胜率 **85.37%**，为东吴全系列最强

### 6.6 改进全景对比

| 因子 | IR | 胜率 | 最大回撤 | 增量来源 |
|------|:--:|:---:|:------:|---------|
| Turn20 | 1.90 | 71.36% | 15.53% | 基准 |
| **STR** | **2.96** | 77.60% | 10.05% | 稳定性 |
| SCR | ~1.5 | — | — | 时序稳定性 |
| UTR | 3.07 | 79.13% | 8.77% | 优加法 |
| UTR 2.0 | **3.21** | 82.04% | 9.27% | 连续优加法 |
| SPS | **3.27** | 83.54% | 11.58% | 影线差价量配合 |
| SPS_Turbo | **3.91** | 85.37% | 10.22% | +GTR |

---

## 七、数据要求

### 仅需日频换手率

| 数据字段 | 频率 | 获取方式 |
|---------|------|---------|
| 日换手率 | 日频 | `akshare.stock_zh_a_hist()` — 免费 |
| 流通市值 | 日频 | akshare — 免费（中性化用） |
| 申万行业分类 | — | akshare — 免费 |

**完全通过 akshare 免费实现**。无需分钟级数据，无需 Level-2。

---

## 八、推荐实现方案

### Python 实现

```python
import akshare as ak
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

def compute_STR_factor(df: pd.DataFrame, window: int = 20):
    """
    Compute STR (Stability of Turnover Rate) factor.
    
    Args:
        df: DataFrame with columns [date, code, turnover, market_cap, industry]
            turnover = daily turnover rate (as percentage or decimal)
        window: rolling window in trading days (default 20)
    
    Returns:
        DataFrame with monthly STR factor values per stock
    """
    # Step 1: Compute rolling std of daily turnover (20-day window per stock)
    df = df.sort_values(['code', 'date'])
    df['turn_std'] = df.groupby('code')['turnover'].transform(
        lambda x: x.rolling(window, min_periods=10).std()
    )
    
    # Step 2: Aggregate to month-end
    monthly = df.groupby(['code', df['date'].dt.to_period('M')]).last().reset_index()
    
    # Step 3: Cross-sectional MAD outlier trimming (5x)
    for month, group in monthly.groupby('date'):
        median = group['turn_std'].median()
        mad = (group['turn_std'] - median).abs().median()
        upper = median + 5 * mad
        lower = median - 5 * mad
        monthly.loc[group.index, 'turn_std_trimmed'] = group['turn_std'].clip(lower, upper)
    
    # Step 4: Cross-sectional Z-score
    monthly['STR_z'] = monthly.groupby('date')['turn_std_trimmed'].transform(
        lambda x: (x - x.mean()) / x.std()
    )
    
    # Step 5: Market cap neutralization (cross-sectional per month)
    monthly['log_mcap'] = np.log(monthly['market_cap'])
    residuals = []
    for month, group in monthly.groupby('date'):
        y = group['STR_z'].values.reshape(-1, 1)
        X = group['log_mcap'].values.reshape(-1, 1)
        pred = LinearRegression().fit(X, y).predict(X)
        resid = y.flatten() - pred.flatten()
        residuals.append(pd.Series(resid, index=group.index))
    monthly['STR_neutral'] = pd.concat(residuals)
    
    # Step 6: Final Z-score + negate
    monthly['STR'] = -monthly.groupby('date')['STR_neutral'].transform(
        lambda x: (x - x.mean()) / x.std()
    )
    
    return monthly[['date', 'code', 'STR']]

# Usage
# df = ak.stock_zh_a_hist(...)  # daily OHLCV+turover for all A stocks
# factor = compute_STR_factor(df, window=20)
# NEGATIVE factor values = sell, POSITIVE = buy (stable turnover = good)
```

### 参数推荐

| 参数 | 推荐值 | 可调范围 | 说明 |
|------|--------|---------|------|
| 窗口 | **20日** | 10-60日 | 对参数不敏感，20日匹配月频 |
| 最小交易日 | **10日** | — | 新上市不满10日不计算 |
| 去极值 | **MAD 5倍** | 3-5倍 | 标准做法 |
| 中性化 | **市值** | 市值+行业 | 基准版市值即可 |
| 调仓频率 | **月频** | 双周/周频 | 视交易成本 |

---

## 九、已知问题

### 9.1 市场状态依赖

| 状态 | STR 表现 | 原因 |
|------|---------|------|
| 震荡市 | 最优 | 稳定换手=优质信号 |
| 趋势牛市 | 略弱 | 放量上涨的股票被误杀 |
| 暴跌/股灾 | 仍有效 | 波动大的确实跌更多 |
| 低换手率环境（2023-2025） | 正常 | 量缩时稳定性仍是有效区分 |

> STR 在2015牛、2016熔断、2018熊、2020-2022全周期内最大回撤仅10%，远优于 Turn20 的15.5%——说明其对市场状态的适应性强于传统换手率因子。

### 9.2 与 Turn20 的高相关性

STR ↔ Turn20 ≈ 0.86，意味着 STR 的大部分信息来自"换手率水平低时波动也小"。如果系统已使用 Turn20，建议对 STR 做正交化处理（STR_deTurn20 = 残差），否则两个因子会高度冗余。

### 9.3 组合容量

海通证券研究：换手率类因子在组合规模超过**30亿**后 IC 显著下降。STR 略优于 Turn20（稳定性逻辑对机构投资者更友好），但仍需关注规模限制。

---

## 十、关键来源

1. **东吴证券**《量稳换手率选股因子——量小、量缩，都不如量稳？》(2021.05.15) — [新浪财经](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=674533759656)
2. **东吴证券**《改进STR：换手率要比别人稳 也要比自己稳》(2021.12.07) — [新浪财经](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/692205775586)
3. **东吴证券**《优加换手率UTR选股因子2.0》(2023.05.05) — [新浪财经](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=736607223183)
4. **东吴证券**《换手率变化率的稳定GTR因子》(2023-2024) — [新浪财经](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/737029599213)
5. **东吴证券**《量稳换手率STR选股因子绩效月报》系列 (2023-2025) — [发现报告](https://www.fxbaogao.com/detail/5140107)
6. **东吴证券**《TPS与SPS选股因子绩效月报》(截至2025.11) — [慧博](https://m.hibor.com.cn/wap_detail.aspx?id=d716ac9b056bfd7f69cd0dad295adc8e)
7. CSDN 复现文章《研报复现 | 量稳换手率选股因子》— [CSDN](https://blog.csdn.net/weixin_42219751/article/details/122131652)
