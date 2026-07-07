# OCFP（经营现金流/市值）因子深度解析

> 2026-07-07 | 华泰证券《单因子测试之估值类因子》(2016.09.29) + 华泰行业轮动系列(2020)

---

## 一、精确公式

$$\boxed{OCFP = \frac{\text{经营活动产生的现金流量净额 (TTM)}}{\text{总市值}}}$$

### 1.1 分子：经营活动产生的现金流量净额

**对应财报科目**：现金流量表中"**经营活动产生的现金流量净额**"

| 语言 | 术语 |
|------|------|
| 中文（标准） | 经营活动产生的现金流量净额 |
| 中文（简称） | 经营现金流净额 / 经营性净现金流 |
| 英文 | Net Operating Cash Flow / Cash Flow from Operations |
| akshare 字段名 | `NETOPERATE_CASH_FLOW` 或 `net_operate_cash_flow` |
| Wind 字段 | `FFO` / `net_cash_flows_oper_ttm` |

### 1.2 分母：总市值

华泰证券标准做法使用**总市值**（非流通市值）：

$$总市值 = 收盘价 \times 总股本$$

### 1.3 TTM 年化方式

OCFP 使用 **TTM（Trailing Twelve Months）**，即最近四个季度（单季报表）的经营活动现金流净额之和，或直接使用最新年报/季报中已披露的TTM值：

$$OCFP_{TTM} = \frac{\sum_{q=t-3}^{t} CFO_q}{MktCap_t}$$

其中 $CFO_q$ 为第 $q$ 季度的经营活动现金流净额（单位：元）。

### 1.4 符号方向

| 方向 | 含义 |
|:---:|------|
| **正向（+）** | OCFP 越高 → 每股经营现金流越多 → **低估信号** → 预期未来收益越高 |
| 逻辑 | 经营现金流是净利润的"质量验证"——现金流高的公司利润更真实 |

华泰所有估值因子除 EV2EBITDA（方向-1）外均为**正向因子**。

---

## 二、数据源映射

### 2.1 akshare 接口

```python
import akshare as ak

# 方法1：东方财富现金流量表（按报告期）
df = ak.stock_cash_flow_sheet_em(symbol="600519")

# 方法2：新浪财经现金流量表
df = ak.stock_cash_flow_sheet_report_sina(symbol="600519")
```

### 2.2 字段对应关系

| 财务概念 | akshare (东财) 典型列名 | 说明 |
|---------|----------------------|------|
| 经营活动产生的现金流量净额 | `NET_OPERATE_CASH_FLOW` | **核心字段** |
| 销售商品、提供劳务收到的现金 | `SALES_SERVICES` | 辅助验证 |
| 投资活动产生的现金流量净额 | `NET_INVEST_CASH_FLOW` | 区分OCF和FCF |
| 购建固定资产、无形资产… | `PURCHASE_FIXED_ASSETS` | 计算FCF用 |
| 筹资活动产生的现金流量净额 | `NET_FINANCE_CASH_FLOW` | — |
| 期末现金及现金等价物余额 | `CASH_END_PERIOD` | — |

### 2.3 确认方法

如果字段名不确定，验证公式为：
> **NET_OPERATE_CASH_FLOW** 应约等于 销售商品收到的现金 − 购买商品支付的现金 − 支付给职工的现金 − 支付的各项税费 + 其他经营收入 − 其他经营支出

或更简单：**净利润** 与 **NET_OPERATE_CASH_FLOW** 的差额应等于非现金项目（折旧、应收应付变动等）之和。

---

## 三、出处

### 3.1 核心来源

| 项目 | 内容 |
|------|------|
| **标题** | 《单因子测试之估值类因子》 |
| **系列** | 华泰证券多因子系列之二 |
| **日期** | **2016年9月29日** |
| **分析师** | 林晓明、陈烨 |
| **链接** | [BigQuant](https://bigquant.com/wiki/doc/SSIL9IGlAH) / [新浪财经](http://mp.weixin.qq.com/s?__biz=Mzg2NjUyODY0Mw==&mid=2247484050&idx=1&sn=667cd45cb3cb52fea4fa94e6691de896) |

### 3.2 后续更新

| 来源 | 贡献 |
|------|------|
| **华泰行业轮动系列 (2020)** | 按盈利模式分集群测试OCFP，发现低费用型企业ICIR最高(2.32) |
| **华泰《历史分位数因子》(2019)** | ts_rank(OCFP, n) 变体，与原始OCFP相关性仅16% |
| **华泰《如何使价值因子更具"价值"》(2024-2025)** | IBP/ENOA等改进版价值因子 |

---

## 四、实证数据

### 4.1 华泰分层回测（2016年，全A十行业内部排序）

| 评估维度 | OCFP 排名（10个估值因子中） |
|---------|:---:|
| **TOP组合收益** | 🥈 前4（仅次于BP/SP/NCFP） |
| **TOP组合IR** | 🥈 前4 |
| **回撤控制** | 🥉 前3（仅次于BP/DP） |
| **多空组合收益** | 🥉 前3（仅次于BP/NCFP） |

### 4.2 华泰 IC值分析

| 指标 | 结果 |
|------|:---:|
| **是否通过有效性检验** | ✅ 通过（vs FCFP被建议删去） |
| **因子收益率 t值** | 显著 |
| **IC方向** | 正 |
| **ICIR** | 中上——为所有**静态**估值因子中最高（高于EP和BP） |

### 4.3 华泰行业轮动系列（2020年，按盈利模式集群）

| 企业集群 | OCFP 的 ICIR |
|---------|:---:|
| 低费用型 | **2.32** 🔥 |
| 高周转型 | 1.93 |
| 高净利型 | 1.65 |
| 高杠杆型 | 1.19 |

### 4.4 典型行业示例（机械行业）

| 指标 | 数值 |
|------|:---:|
| RankIC均值 | 3.7% |
| RankIR | **1.39** |
| 多头年化 | 11.3% |
| 空头年化 | 1.0% |
| 多空超额年化 | ~10.3% |

### 4.5 vs FCFP 的关键对比

| 因子 | 华泰评价 | 原因 |
|------|:---:|------|
| **OCFP** | ✅ **保留** | IC稳定、多空优秀、回撤控制好 |
| **FCFP** | ❌ **删去** | IC不稳定(ICIR=0.174)、t值不显著、胜率仅58.85% |
| **NCFP** | ⚠️ 谨慎使用 | 分层测试表现好(精选前20%有效)，但IC分析不佳 |

> 华泰原话："在回归统计和IC值分析框架下，NCFP、FCFP效果不佳可以删去"

**OCFP胜出FCFP的原因**：
1. FCFP的分母（FCF = OCF - CAPEX）增加了资本开支的噪音——A股公司年度CAPEX变化极大
2. 经营现金流比自由现金流**更稳定**、季度波动更小
3. FCFP的IC胜率仅58.85%（接近随机），OCFP的IC稳定性显著更好

---

## 五、与其他估值因子的相关性

### 5.1 同类别相关性（华泰 2016）

| 因子对 | 相关性 |
|------|:---:|
| OCFP ↔ BP | 中高正相关 |
| OCFP ↔ EP | 中高正相关 |
| OCFP ↔ SP | 中等正相关 |
| OCFP ↔ DP（股息率） | 中高正相关 |
| OCFP ↔ NCFP | 高正相关（同是现金流类） |
| OCFP ↔ ln（市值） | 正相关（~0.19），大盘股OCFP更高 |

### 5.2 时序分位数变体的相关性

| 因子对 | 相关性 |
|------|:---:|
| ts_rank(OCFP, 2) ↔ 原始 OCFP | **16.01%**（很低，说明分位数化后大幅改变了因子结构） |
| ts_rank(OCFP, 2) ↔ ts_rank(EP, 2) | 27.46% |
| ts_rank(OCFP, 2) ↔ ts_rank(BP, 2) | 26.77% |
| ts_rank(OCFP, 2) ↔ ln（市值） | -4.09% |

> 市值中性化效果：原始OCFP与市值正相关(~19%)，分位数化后降至-4%，说明时序分位数化可以同时完成市值中性化。

---

## 六、已知问题

### 6.1 季频更新的滞后效应（关键）

这是所有财报因子（包括OCFP）**最大的工程问题**：

| 报告期 | 截止日期 | 法定披露截止日 | 实际可用日期 | 滞后 |
|--------|---------|:---:|---------|:---:|
| 一季报 | 3月31日 | **4月30日** | 5月初调仓 | ~1个月 |
| 中报 | 6月30日 | **8月31日** | 9月初调仓 | ~2个月 |
| 三季报 | 9月30日 | **10月31日** | 11月初调仓 | ~1个月 |
| 年报 | 12月31日 | **次年4月30日** | 5月初调仓 | **~4个月** |

**工程处理方案**：

| 方案 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A: 滞后一期**（标准做法） | 4月底调仓使用截至3月31日的TTM数据 | 无前视偏差 | 信号滞后~1个月 |
| **B: 截止日对齐** | 直接用报告期截止日的TTM，不考虑披露延迟 | 简单 | **前视偏差**（用了未公开信息） |
| **C: 滚动更新** | 每月用已公布的最新季报TTM | 最大化时效 | Q1信号最旧（年报数据滞后4个月） |

> **推荐方案A**：在调仓日（如5月第一个交易日），只使用**披露截止日已到**的最新季报。即5-8月调仓用一季报数据（TTM = Q1-Q4），9-10月用中报数据（TTM = Q2-Q3），11-4月用三季报+年报。

### 6.2 负现金流的处理

部分行业（重资产周期性、初创成长型）经常出现负的经营现金流：

| 处理方式 | 推荐度 | 说明 |
|---------|:---:|------|
| **保留负值，参与截面排序** | ✅ 推荐 | 负OCFP=最差的组，作为空头信号有效 |
| 设为缺失值 | ❌ | 损失信息，且会系统性地排除高成长公司 |
| 截尾为0 | ❌ | 人为制造分布断点 |
| 加绝对最小值平移 | ⚠️ 仅在rank IC测试中适用 | 使所有因子值>0，但不改变排序 |

> OCFP为负本身就是**强空头信号**——负经营现金流的公司未来收益显著低于正现金流公司。保留负值即可。

### 6.3 金融股的特殊性

| 行业 | 问题 | 建议 |
|------|------|------|
| **银行** | 银行的经营现金流概念与非金融企业完全不同（存款算流入、贷款算流出），OCFP对银行无效 | 剔除银行 |
| **非银金融**（券商/保险） | 现金流受自营交易、承销等波动影响极大，季度间不可比 | 剔除或单独建模 |
| **房地产** | 开发支出计入经营现金流（A股特色），扭曲OCF/净利润关系 | 谨慎使用 |

### 6.4 季节性效应

A股经营活动现金流存在显著**季节性**：
- Q4 经营现金流**异常高**（年底回款集中、催收效应）
- Q1 经营现金流**异常低**（春节期间停工、年初集中采购）

使用TTM（最近四个季度滚动求和）可部分平滑季节效应，但如果用单季度数据直接年化（×4），季度的IC会大幅衰减。**TTM是必需的，不可用单季数据代替**。

### 6.5 行业中性化必要性

OCFP在不同行业间水平差异极大（重资产行业天然现金流充沛，轻资产行业现金流弱）。**必须做行业中性化**（申万一级哑变量回归取残差），否则选出的永远只是现金流充沛的行业（能源、建材、公用事业），而非行业内的优质个股。

---

## 七、与 EP 和 FCF/EV 的对比总结

| 维度 | OCFP | EP（净利润/市值） | FCF/EV |
|------|:---:|:---:|:---:|
| **分子质量** | 高（经营现金流难造假） | 中（净利润可被会计操纵） | 中高（自由现金流较真实但波动大） |
| **ICIR** | **0.526** ✅（静态估值中最高） | 0.3-0.6 | 0.174 ❌（华泰建议删去） |
| **稳定性** | ✅ 最稳定 | ⚠️ 受一次性损益干扰 | ❌ 受CAPEX波动影响大 |
| **行业覆盖** | 全行业（剔除金融地产） | 全行业 | 仅大盘股（中证1000中ICIR=3.89%） |
| **数据源** | 现金流量表季报 | 利润表季报 | 现金流量表+CAPEX |
| **最佳场景** | 低费用型企业（ICIR=2.32） | 盈利稳定型 | 大盘价值 |
| **劣势** | 季节效应强、季频更新慢 | 易被会计操纵 | IC不稳定、小盘无效 |

---

## 八、推荐实现

```python
import akshare as ak
import numpy as np
import pandas as pd

def compute_OCFP_factor(codes, current_date, lookback_quarters=4):
    """
    Compute OCFP factor from akshare cash flow data.
    
    Args:
        codes: list of stock codes (e.g. ['600519', '000858'])
        current_date: rebalance date (e.g. '2025-05-05')
        lookback_quarters: number of quarters for TTM (default 4)
    
    Returns:
        DataFrame with [code, OCFP, OCFP_industry_neutral]
    """
    results = []
    current_dt = pd.Timestamp(current_date)
    
    for code in codes:
        try:
            # Step 1: Get cash flow statement
            cf = ak.stock_cash_flow_sheet_em(symbol=code)
            
            # Step 2: Filter to quarters BEFORE the current rebalance date
            # that have passed their disclosure deadline
            cf['报告期'] = pd.to_datetime(cf['报告期'])
            cf = cf[cf['报告期'] <= current_dt].sort_values('报告期')
            
            # Step 3: Get TTM operating cash flow (last N quarters sum)
            ocf_quarterly = cf['NET_OPERATE_CASH_FLOW'].tail(lookback_quarters)
            ocf_ttm = ocf_quarterly.sum()
            
            # Step 4: Get market cap
            market_cap = cf['TOTAL_MARKET_CAP'].iloc[-1]  # or get from daily data
            
            # Step 5: OCFP
            ocfp = ocf_ttm / market_cap if market_cap > 0 else np.nan
            
            results.append({'code': code, 'OCFP': ocfp})
        except:
            continue
    
    df = pd.DataFrame(results)
    
    # Step 6: Cross-sectional z-score
    df['OCFP_z'] = (df['OCFP'] - df['OCFP'].mean()) / df['OCFP'].std()
    
    # Step 7: Industry neutralization (pseudo-code)
    # df = merge with industry classification
    # for industry in industries:
    #     mask = df['industry'] == industry
    #     df.loc[mask, 'OCFP_ind_neutral'] = df.loc[mask, 'OCFP_z'] - df.loc[mask, 'OCFP_z'].mean()
    
    # OCFP higher = better (value signal)
    return df
```

### 参数推荐

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| TTM 窗口 | **4个季度** | 标准做法 |
| 市值定义 | **总市值** | 华泰标准 |
| 调仓频率 | **季频** 或 月频(用滞后TTM) | 4/5/9/11月 |
| 行业中性化 | **申万一级** | 必需 |
| 负值处理 | **保留**（参与排序） | 不做截尾 |
| 剔除金融 | **是** | 银行/非银/地产 |

---

## 九、关键来源

1. **华泰证券 林晓明/陈烨**《单因子测试之估值类因子》(2016.09.29) — [BigQuant](https://bigquant.com/wiki/doc/SSIL9IGlAH)
2. **华泰证券**《行业轮动系列》(2020) — OCFP按盈利模式集群测试
3. **华泰证券**《历史分位数因子》(2019) — ts_rank(OCFP, n) 改进版
4. **华泰证券**《如何使价值因子更具"价值"》(2024-2025) — IBP, RIMVP等改进
5. **factors.directory** — [经营性现金流市值比](https://factors.directory/zh/factors/basic-surface/cash-flow-to-market-value) 标准化定义
6. **Chordia, Subrahmanyam & Anshuman (2001)**, *JFE* — 交易活动与预期收益
