# 数据字典 — quant 量化选股系统

## 概述

系统使用两个 SQLite 数据库：

| 数据库 | 文件名 | 中文名 | 作用 |
|--------|--------|--------|------|
| market.db | `data/market.db` | 市场数据库 | 存储行情日线、基本面、因子评估、资金流等市场相关数据，是系统的数据仓库 |
| trades.db | `data/trades.db` | 交易数据库 | 存储模拟交易记录和策略配置，是交易执行的唯一真相源（Single Source of Truth） |

---

## market.db — 市场数据库

### factor_registry — 因子注册表

**作用**：因子的元数据中心。记录每个因子"是什么、谁写的、当前状态、IC 评估结果"。pipeline 通过 `status='active'` 决定哪些因子参与信号生成，`compute_factor_stats()` 自动更新 `ic_mean`/`ic_ir`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | TEXT PK | 因子名称，如 `zt_streak`、`gap_5d` |
| `category` | TEXT | 因子分类：momentum / value / profitability / limit_up 等 |
| `compute_fn` | TEXT | 对应的 Python 计算函数名 |
| `academic_source` | TEXT | 学术文献出处，如 "Fama-French (1992)" |
| `status` | TEXT | `active`=启用中，`deprecated`=已失效。只有 active 的因子参与 pipeline |
| `status_reason` | TEXT | 当前状态的简要原因，如 "stepwise evaluation" / "passed stepwise backtest" |
| `notes` | TEXT | 状态变更历史记录（追加式），格式: `YYYY-MM-DD HH:MM \| 操作 \| 原因` |
| `ic_mean` | REAL | 最近 120 个交易日截面 IC 均值（`compute_factor_stats()` 写入） |
| `ic_ir` | REAL | IC / IC波动率 = Information Ratio（同上） |
| `direction` | TEXT | 因子预期方向：`positive`=高分偏好，`negative`=低分偏好 |
| `last_evaluated` | TEXT | 最近一次 IC 评估的时间戳 |
| `created_at` | TEXT | 因子首次注册时间 |
| `updated_at` | TEXT | 最后一次修改时间 |

---

### factor_snapshot — 因子评估快照表

**作用**：缓存 `compute_factor_stats()` 的完整输出（IC 向量 + 相关性矩阵 + 排序结果），24 小时有效。前端 `/api/factors` 接口从此表读取，避免每次请求都执行 3 分钟的全量计算。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 固定为 1，单行表 |
| `data` | TEXT | JSON 格式缓存：包含 ic、ic_ir、corr 矩阵、factor_keys、sorting 等 |
| `created_at` | TEXT | 快照生成时间，超过 24 小时自动过期重算 |
| `n_symbols` | INTEGER | 评估时使用的截面股票数（来自 config: `factor.evaluation.n_symbols`） |
| `lookback` | INTEGER | 评估时使用的交易日窗口（来自 config: `factor.evaluation.lookback`） |

---

### daily — 日线行情表

**作用**：存储全 A 股每日 OHLCV 行情数据。是系统最核心的数据表，因子计算、回测、估值均依赖此表。数据通过 `store.update_daily()` 增量拉取（优先 akshare，回退腾讯财经）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码，如 `600036` |
| `date` | TEXT | 交易日期，格式 YYYY-MM-DD |
| `open` | REAL | 开盘价 |
| `high` | REAL | 最高价 |
| `low` | REAL | 最低价 |
| `close` | REAL | 收盘价 |
| `volume` | REAL | 成交量（股） |
| `amount` | REAL | 成交额（元） |
| `turnover` | REAL | 换手率（%） |

---

### stocks — 股票基本信息表

**作用**：存储全 A 股列表、名称、市值、行业分类等静态/慢变数据。`store.update_stock_list()` 定期同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT PK | 股票代码 |
| `name` | TEXT | 股票名称 |
| `market` | TEXT | 所属市场：SH（上海）/ SZ（深圳）/ BJ（北交所） |
| `list_date` | TEXT | 上市日期 |
| `pe` | REAL | 静态市盈率 |
| `pe_ttm` | REAL | 滚动市盈率（TTM） |
| `pb` | REAL | 市净率 |
| `total_mv` | REAL | 总市值 |
| `circ_mv` | REAL | 流通市值 |
| `div_yield` | REAL | 股息率 |
| `eps` | REAL | 每股收益 |
| `bvps` | REAL | 每股净资产 |
| `cfps` | REAL | 每股现金流 |
| `high_52w` | REAL | 52 周最高价 |
| `low_52w` | REAL | 52 周最低价 |
| `turnover_rate` | REAL | 换手率 |
| `industry` | TEXT | 行业分类 |
| `roe` | REAL | 净资产收益率 |
| `total_shares` | REAL | 总股本 |

---

### financial_income — 利润表

**作用**：存储个股季度利润表数据（营业收入、净利润等），用于计算 ROE、资产增长率等基本面因子。通过 akshare 同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `stat_date` | TEXT | 统计截止日期 |
| `pub_date` | TEXT | 公告发布日期 |
| `total_operating_revenue` | REAL | 营业总收入 |
| `operating_revenue` | REAL | 营业收入 |
| `operating_cost` | REAL | 营业成本 |
| `operating_profit` | REAL | 营业利润 |
| `net_profit` | REAL | 净利润 |
| `total_profit` | REAL | 利润总额 |
| `income_tax_expense` | REAL | 所得税费用 |
| `administration_expense` | REAL | 管理费用 |

---

### financial_balance — 资产负债表

**作用**：存储个股季度资产负债表数据，用于计算资产负债率、应计利润等因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `stat_date` | TEXT | 统计截止日期 |
| `pub_date` | TEXT | 公告发布日期 |
| `total_assets` | REAL | 总资产 |
| `total_liability` | REAL | 总负债 |
| `total_owner_equities` | REAL | 股东权益合计 |
| `equities_parent_company_owners` | REAL | 归属母公司股东权益 |
| `minority_interests` | REAL | 少数股东权益 |
| `fixed_assets` | REAL | 固定资产 |
| `intangible_assets` | REAL | 无形资产 |
| `good_will` | REAL | 商誉 |
| `inventories` | REAL | 存货 |
| `account_receivable` | REAL | 应收账款 |
| `total_current_assets` | REAL | 流动资产合计 |
| `total_current_liability` | REAL | 流动负债合计 |
| `shortterm_loan` | REAL | 短期借款 |
| `longterm_loan` | REAL | 长期借款 |

---

### financial_cash_flow — 现金流量表

**作用**：存储个股季度现金流量表数据，用于计算应计利润等质量因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `stat_date` | TEXT | 统计截止日期 |
| `pub_date` | TEXT | 公告发布日期 |
| `net_operate_cash_flow` | REAL | 经营活动现金流净额 |
| `net_invest_cash_flow` | REAL | 投资活动现金流净额 |
| `net_finance_cash_flow` | REAL | 筹资活动现金流净额 |
| `cash_and_equivalents_at_end` | REAL | 期末现金及等价物余额 |
| `goods_sale_and_service_render_cash` | REAL | 销售商品/提供劳务收到的现金 |
| `fix_intan_other_asset_acqui_cash` | REAL | 购建固定资产/无形资产等支付的现金 |

---

### daily_basic — 每日基本面指标表

**作用**：存储每只股票每日的基本面估值指标（PE_TTM、PB），用于计算 ep_ratio、bp_ratio 等价值因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `date` | TEXT | 日期 |
| `close` | REAL | 收盘价 |
| `pe_ttm` | REAL | 滚动市盈率 |
| `pb` | REAL | 市净率 |

---

### daily_valuation — 每日估值表（JQData 源）

**作用**：通过 JQData 获取的每日估值数据（PE/PB/PS/PCF 等），比 daily_basic 字段更全。JQData trial 截止 2026-04-02 后作为回退源。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `date` | TEXT | 日期 |
| `pe_ttm` | REAL | 滚动市盈率 |
| `pb` | REAL | 市净率 |
| `ps_ttm` | REAL | 滚动市销率 |
| `pcf_ttm` | REAL | 滚动市现率 |
| `market_cap` | REAL | 总市值 |
| `turnover_rate` | REAL | 换手率 |
| `source` | TEXT | 数据来源，默认 `jqdata` |

---

### northbound_flow — 北向资金流向表

**作用**：存储沪深港通每日北向资金净买入数据，用于计算 `northbound_20d` 因子。数据通过 akshare 同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | TEXT | 日期 |
| `symbol` | TEXT | 股票代码 |
| `net_buy` | REAL | 当日净买入额 |
| `buy_amt` | REAL | 买入金额 |
| `sell_amt` | REAL | 卖出金额 |
| `hold_shares` | REAL | 持股数量 |
| `hold_ratio` | REAL | 持股比例 |

---

### margin_detail — 融资融券明细表

**作用**：存储个股每日融资融券交易数据，用于计算 `margin_balance_chg`、`margin_buy_ratio` 等杠杆资金因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `date` | TEXT | 日期 |
| `market` | TEXT | 市场 |
| `margin_buy` | REAL | 融资买入额 |
| `margin_balance` | REAL | 融资余额 |
| `margin_repay` | REAL | 融资偿还额 |
| `short_sell_vol` | REAL | 融券卖出量 |
| `short_balance` | REAL | 融券余额 |
| `short_total` | REAL | 融券总量 |
| `margin_total` | REAL | 两融余额合计 |

---

### lhb_detail — 龙虎榜明细表

**作用**：存储个股龙虎榜上榜明细数据，用于计算 `lhb_net_buy_20d`、`lhb_post_quality` 等龙虎榜因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `trade_date` | TEXT | 上榜日期 |
| `close` | REAL | 收盘价 |
| `change_pct` | REAL | 涨跌幅 |
| `turnover_rate` | REAL | 换手率 |
| `net_buy` | REAL | 龙虎榜净买入 |
| `buy_amt` | REAL | 买入金额 |
| `sell_amt` | REAL | 卖出金额 |
| `reason` | TEXT | 上榜原因 |
| `name` | TEXT | 股票名称 |
| `circ_mv` | REAL | 流通市值 |
| `post_1d` | REAL | 上榜后 1 日收益 |
| `post_2d` | REAL | 上榜后 2 日收益 |
| `post_5d` | REAL | 上榜后 5 日收益 |
| `post_10d` | REAL | 上榜后 10 日收益 |

---

### limit_up_pool — 涨停板池表

**作用**：存储每日涨停股票池，用于计算 `zt_streak`、`limit_up_prox_5d` 等涨跌停板因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | TEXT | 日期 |
| `symbol` | TEXT | 股票代码 |
| `name` | TEXT | 股票名称 |
| `change_pct` | REAL | 涨跌幅 |
| `close` | REAL | 收盘价 |
| `amount` | REAL | 成交额 |
| `circ_mv` | REAL | 流通市值 |
| `total_mv` | REAL | 总市值 |
| `turnover_rate` | REAL | 换手率 |
| `lock_capital` | REAL | 封单资金 |
| `first_time` | TEXT | 首次涨停时间 |
| `last_time` | TEXT | 最后涨停时间 |
| `open_times` | INTEGER | 开板次数 |
| `zt_stat` | TEXT | 涨停状态 |
| `limit_up_times` | INTEGER | 连续涨停天数 |
| `industry` | TEXT | 所属行业 |

---

### fund_hold — 基金持仓表

**作用**：存储基金持仓变动数据，用于计算 `fund_change` 因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `report_date` | TEXT | 报告期 |
| `fund_count` | INTEGER | 持有基金数量 |
| `hold_shares` | REAL | 持仓股数 |
| `hold_mv` | REAL | 持仓市值 |
| `change_type` | TEXT | 变动方向 |
| `change_ratio` | REAL | 变动比例 |

---

### dividend — 分红送转表

**作用**：存储个股分红、送转、配股数据，用于计算 `dividend_yield` 因子。数据通过 akshare 同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `end_date` | TEXT | 报告期截止日 |
| `div_year` | INTEGER | 分红年度 |
| `cash_div` | REAL | 每股现金分红 |
| `stk_div` | REAL | 每股送转比例 |
| `record_date` | TEXT | 股权登记日 |
| `ex_date` | TEXT | 除权除息日 |

---

### holder_trade — 大股东增减持表

**作用**：存储大股东/高管增减持数据，用于计算 `holder_reduction` 因子。数据通过 akshare 同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `ann_date` | TEXT | 公告日期 |
| `holder_name` | TEXT | 股东名称 |
| `holder_type` | TEXT | 股东类型 |
| `change_vol` | REAL | 变动数量（股） |
| `change_ratio` | REAL | 变动比例 |
| `direction` | TEXT | 变动方向：增持/减持 |
| `avg_price` | REAL | 均价 |

---

### pledge_stat — 股权质押统计表

**作用**：存储个股股权质押统计数据，用于计算 `pledge_ratio` 因子。数据通过 akshare 同步。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `end_date` | TEXT | 统计截止日 |
| `pledge_times` | INTEGER | 质押次数 |
| `pledge_shares` | REAL | 质押股数 |
| `pledge_amount` | REAL | 质押金额 |
| `total_shares` | REAL | 总股数 |

---

### analyst_forecast — 分析师预测表

**作用**：存储分析师一致预期数据（EPS 预测、评级分布），用于计算 `analyst_buy` 因子。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 股票代码 |
| `sync_date` | TEXT | 同步日期 |
| `report_count` | INTEGER | 报告总数 |
| `buy_count` | INTEGER | 买入评级数 |
| `overweight_count` | INTEGER | 增持评级数 |
| `neutral_count` | INTEGER | 中性评级数 |
| `underweight_count` | INTEGER | 减持评级数 |
| `eps_2026` | REAL | 2026 年一致预期 EPS |
| `eps_2027` | REAL | 2027 年一致预期 EPS |
| `eps_2028` | REAL | 2028 年一致预期 EPS |

---

### benchmark_daily — 基准指数日线表

**作用**：存储沪深 300 等基准指数的日线数据，用于回测基准对比和归因分析。

| 字段 | 类型 | 说明 |
|------|------|------|
| `index_code` | TEXT | 指数代码，如 `000300` |
| `date` | TEXT | 交易日期 |
| `open` | REAL | 开盘价 |
| `high` | REAL | 最高价 |
| `low` | REAL | 最低价 |
| `close` | REAL | 收盘价 |
| `volume` | REAL | 成交量 |
| `amount` | REAL | 成交额 |

---

### meta — 元数据表

**作用**：存储数据库级别的元数据键值对，如数据同步状态、版本号等。

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT PK | 键名 |
| `value` | TEXT | 键值 |

---

## trades.db — 交易数据库

### sim_trades — 模拟交易记录表

**作用**：存储每一笔模拟交易的完整记录（买卖方向、价格、股数、盈亏），是交易执行的唯一真相源。`TradeRepo` 是本表的唯一读写入口。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 交易记录 ID，自增 |
| `date` | TEXT | 交易日期，格式 YYYY-MM-DD |
| `symbol` | TEXT | 股票代码 |
| `side` | TEXT | 买卖方向：`buy` 或 `sell`（CHECK 约束） |
| `price` | REAL | 成交价格 |
| `shares` | INTEGER | 成交股数 |
| `pnl` | REAL | 已实现盈亏（仅 sell 时有值） |
| `pnl_pct` | REAL | 已实现盈亏百分比 |
| `capital_after` | REAL | 交易后总资产 |
| `strategy` | TEXT | 策略标识，默认 `quant` |
| `board_count` | INTEGER | 连板数（仅 buy 时记录，用于涨停因子） |
| `created_at` | TEXT | 记录创建时间（UTC） |

---

### strategy_config — 策略配置表

**作用**：存储每个策略的初始资金和当前现金余额。`TradeRepo` 是本表的唯一读写入口。每笔交易后 `cash_balance` 原子更新。

| 字段 | 类型 | 说明 |
|------|------|------|
| `strategy` | TEXT PK | 策略标识，如 `quant` |
| `initial_capital` | REAL | 种子本金（¥5,000），首次启动时写入，永不修改 |
| `cash_balance` | REAL | 当前现金余额，每笔交易后原子更新 |
| `initialized` | INTEGER | 是否已初始化（0/1），防止亏完后重复种子 |
| `updated_at` | TEXT | 最后更新时间 |
