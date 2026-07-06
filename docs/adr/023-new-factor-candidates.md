# ADR 023: A 股已验证新因子候选清单

**日期**: 2026-07-07
**背景**: 现有 36 因子池中仅 2 个有效 (zt_streak, dt_streak)，不再继续挖掘剩余 34 个。
          转向集成 A 股已被量化公司/软件/文献验证有效的新因子。
**关联**: ADR 020 (151 Strategies), HANDOFF.md

---

## Tier 1 — A 股证据强，集成后大概率有效

### 1. Asset Growth（资产增长率）
- **来源**: Cooper, Gulen & Schill (2008) "Asset Growth and the Cross-Section of Stock Returns"; 华泰金工 2023
- **公式**: AG = (TA_t - TA_{t-1}) / TA_{t-1}, TA = 总资产
- **原理**: 总资产增速与未来收益负相关。资产快速扩张的公司往往有过度投资倾向。
- **A 股证据**: IC ≈ -0.03~-0.05
- **所需数据**: 季报总资产 (CSMAR: total_assets, Tushare: total_assets)

### 2. GP/TA（毛利润/总资产）
- **来源**: Novy-Marx (2013) "The Other Side of Value: Good Growth and the Gross Profitability Premium"; Fama-French 2015 RMW
- **公式**: GP/TA = (营业收入 - 营业成本) / 总资产
- **原理**: 高毛利意味着强竞争壁垒，比 ROE/ROA 更纯净（不受杠杆和税率干扰）
- **A 股证据**: 高毛利组合年化超额 6-8%
- **所需数据**: 季报营业收入/营业成本/总资产 (CSMAR: revenue, cost_of_sales, total_assets)

### 3. SUE（标准化未预期盈余）
- **来源**: Bernard & Thomas (1989) "Post-Earnings-Announcement Drift"; 中信金工 2022
- **公式**: SUE = (EPS_t - EPS_{t-4}) / σ(EPS)
- **原理**: 盈余公告后价格存在漂移，超预期股票持续上涨 (PEAD)
- **A 股证据**: 季报后 3-6 月超额收益明显，公告后 60 日窗口 IC > 0.02
- **所需数据**: 季度 EPS, 发布日期 (CSMAR: eps, announcement_date)

### 4. 停牌比率（Zero Trading Days）
- **来源**: Liu (2006) "Liquidity in Chinese Stock Market"; 针对中国市场的流动性度量
- **公式**: ZTD = 过去 250 交易日中零成交天数 / 250
- **原理**: A 股停牌是流动性风险的直接度量，比 Amihud 更适配中国市场特征
- **A 股证据**: 高停牌比率股票显著折价
- **所需数据**: 日线成交量 (已有: market.db daily.volume)

### 5. 大股东减持
- **来源**: 上交所 2020 研究; 海通金工 2023
- **公式**: 大股东减持金额 / 流通市值 (过去 60 日累计)
- **原理**: 大股东接近公司信息源，减持信号包含负面内幕信息
- **A 股证据**: 减持公告后 3-6 月显著负超额收益
- **所需数据**: 大股东减持公告 (CSMAR: major_shareholder_reduction)

### 6. 股权质押比例
- **来源**: 中信建投 2022
- **公式**: Pledge = 大股东质押股数 / 大股东持股总数
- **原理**: 高质押 → 质押预警线/平仓线风险 → 股价崩盘风险溢价
- **A 股证据**: 高质押比例股票波动率溢价显著
- **所需数据**: 大股东质押数据 (CSMAR: pledge_ratio)

---

## Tier 2 — 有证据但需确认数据可获取性

### 7. Industry Momentum（行业动量）
- **来源**: Moskowitz & Grinblatt (1999)
- **原理**: 申万行业层面动量显著强于个股层面
- **所需数据**: 申万行业分类 + 行业指数日线

### 8. Piotroski F-Score
- **来源**: Piotroski (2000)
- **所需数据**: 9 项财务指标 (已有部分: ROA/accruals/debt_ratio)

### 9. 股息率
- **来源**: 中信金工 2023
- **所需数据**: 季报分红数据 (CSMAR: dividend_yield 或 Tushare: dv_ratio)

### 10. 沪深港通资金流
- **来源**: 华泰 2023, 中金 2022
- **所需数据**: 北向资金日流向 (Tushare: moneyflow_hsgt)

---

## Tier 3 — 暂缓

### 11. 融资融券余额变化 → 需要两融日数据
### 12. 龙虎榜席位类型细分 → 已有 lhb_net_buy 但效果差，需细分

---

## 数据源字段对照 (待确认)
详见 docs/data-sources/new-factor-data-requirements.md
