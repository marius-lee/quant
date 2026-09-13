#!/usr/bin/env python3
"""注册另类数据因子到 factor_registry.

用法:
    PYTHONPATH=. .venv/bin/python scripts/register_alternative_factors.py

因子来源: quant/factor/compute/alternative.py
表依赖: research_report, esg_score, macro_high_freq
"""

import sqlite3
from datetime import datetime
from quant.config.paths import MARKET_DB

ALTERNATIVE_FACTORS = [
    # 研报因子
    ("alt_rpt_sentiment", "alternative", "compute_alt_rpt_sentiment",
     "研报情感因子: 单日研报情感均值, 来源 eastmoney 研报标题+摘要 SnowNLP 分析",
     "positive"),
    ("alt_rpt_target_price", "alternative", "compute_alt_rpt_target_price",
     "研报目标价因子: 目标价/当前价 - 1 隐含上涨空间",
     "positive"),
    ("alt_rpt_rating", "alternative", "compute_alt_rpt_rating",
     "研报评级因子: 共识评级得分 (-1 卖出 到 1 买入)",
     "positive"),
    ("alt_rpt_consensus", "alternative", "compute_alt_rpt_consensus",
     "研报共识因子: 近 20 日机构评级均值, 反映机构共识强度",
     "positive"),
    # ESG 因子
    ("alt_esg", "alternative", "compute_alt_esg",
     "ESG 综合评分因子: 华证/MSCI ESG 综合评分",
     "positive"),
    ("alt_env", "alternative", "compute_alt_env",
     "环境评分因子: ESG 中环境维度评分",
     "positive"),
    ("alt_social", "alternative", "compute_alt_social",
     "社会评分因子: ESG 中社会维度评分",
     "positive"),
    ("alt_gov", "alternative", "compute_alt_gov",
     "治理评分因子: ESG 中治理维度评分",
     "positive"),
    ("alt_carbon", "alternative", "compute_alt_carbon",
     "碳排放因子: 碳排放量 (负向, 排放越高越差)",
     "negative"),
    ("alt_green_rev", "alternative", "compute_alt_green_rev",
     "绿色收入占比因子: 绿色业务收入占比",
     "positive"),
    # 宏观高频因子
    ("alt_macro_electricity", "alternative", "compute_alt_macro_electricity",
     "全社会用电量因子: 领先经济活动指标",
     "positive"),
    ("alt_macro_freight", "alternative", "compute_alt_macro_freight",
     "货运指数因子: 货运指数/客货运量合计",
     "positive"),
    ("alt_macro_credit", "alternative", "compute_alt_macro_credit",
     "信贷/社融/货币供应合计因子: 流动性宽松度指标",
     "positive"),
    ("alt_macro_pmi", "alternative", "compute_alt_macro_pmi",
     "PMI 综合因子: 制造业/非制造业/年度 PMI 均值",
     "positive"),
    ("alt_macro_gdp", "alternative", "compute_alt_macro_gdp",
     "GDP 增速因子: 季度/年度 GDP 增速",
     "positive"),
    ("alt_macro_cpi", "alternative", "compute_alt_macro_cpi",
     "CPI 因子: 居民消费价格指数同比",
     "positive"),
    ("alt_macro_ppi", "alternative", "compute_alt_macro_ppi",
     "PPI 因子: 工业生产者出厂价格指数同比",
     "positive"),
    ("alt_macro_m2", "alternative", "compute_alt_macro_m2",
     "M2/货币供应量因子: 广义货币 M2 与货币供应量合计",
     "positive"),
    ("alt_macro_shibor", "alternative", "compute_alt_macro_shibor",
     "Shibor 利率因子: 全期限 Shibor 利率水平",
     "positive"),
    ("alt_macro_lpr", "alternative", "compute_alt_macro_lpr",
     "LPR 因子: 贷款市场报价利率",
     "positive"),
    ("alt_macro_money_supply", "alternative", "compute_alt_macro_money_supply",
     "货币供应量因子: 货币供应量/广义货币",
     "positive"),
    ("alt_macro_bank_financing", "alternative", "compute_alt_macro_bank_financing",
     "银行融资因子: 银行融资/信贷投放",
     "positive"),
    ("alt_macro_industrial", "alternative", "compute_alt_macro_industrial",
     "工业增加值因子: 工业增加值同比增速",
     "positive"),
    ("alt_macro_exports", "alternative", "compute_alt_macro_exports",
     "出口增速因子: 出口同比增速",
     "positive"),
    ("alt_macro_imports", "alternative", "compute_alt_macro_imports",
     "进口增速因子: 进口同比增速",
     "positive"),
    ("alt_macro_retail", "alternative", "compute_alt_macro_retail",
     "社消零售因子: 社会消费品零售总额同比增速",
     "positive"),
    ("alt_macro_real_estate", "alternative", "compute_alt_macro_real_estate",
     "房地产因子: 房地产开发投资/销售",
     "positive"),
    ("alt_macro_traffic", "alternative", "compute_alt_macro_traffic",
     "客货运量因子: 社会客货运量合计",
     "positive"),
    ("alt_macro_shibor", "alternative", "compute_alt_macro_shibor",
     "Shibor 利率因子: 全期限 Shibor 利率水平",
     "positive"),
    ("alt_macro_lpr", "alternative", "compute_alt_macro_lpr",
     "LPR 因子: 贷款市场报价利率",
     "positive"),
    ("alt_macro_money_supply", "alternative", "compute_alt_macro_money_supply",
     "货币供应量因子: 货币供应量/广义货币",
     "positive"),
    ("alt_macro_bank_financing", "alternative", "compute_alt_macro_bank_financing",
     "银行融资因子: 银行融资/信贷投放",
     "positive"),
    ("alt_macro_industrial", "alternative", "compute_alt_macro_industrial",
     "工业增加值因子: 工业增加值同比增速",
     "positive"),
    ("alt_macro_exports", "alternative", "compute_alt_macro_exports",
     "出口增速因子: 出口同比增速",
     "positive"),
    ("alt_macro_imports", "alternative", "compute_alt_macro_imports",
     "进口增速因子: 进口同比增速",
     "positive"),
    ("alt_macro_retail", "alternative", "compute_alt_macro_retail",
     "社消零售因子: 社会消费品零售总额同比增速",
     "positive"),
    ("alt_macro_real_estate", "alternative", "compute_alt_macro_real_estate",
     "房地产因子: 房地产开发投资/销售",
     "positive"),
    ("alt_macro_traffic", "alternative", "compute_alt_macro_traffic",
     "客货运量因子: 社会客货运量合计",
     "positive"),
    ("alt_macro_shibor", "alternative", "compute_alt_macro_shibor",
     "Shibor 利率因子: 全期限 Shibor 利率水平",
     "positive"),
    ("alt_macro_lpr", "alternative", "compute_alt_macro_lpr",
     "LPR 因子: 贷款市场报价利率",
     "positive"),
    ("alt_macro_money_supply", "alternative", "compute_alt_macro_money_supply",
     "货币供应量因子: 货币供应量/广义货币",
     "positive"),
    ("alt_macro_bank_financing", "alternative", "compute_alt_macro_bank_financing",
     "银行融资因子: 银行融资/信贷投放",
     "positive"),
    ("alt_macro_industrial", "alternative", "compute_alt_macro_industrial",
     "工业增加值因子: 工业增加值同比增速",
     "positive"),
    ("alt_macro_exports", "alternative", "compute_alt_macro_exports",
     "出口增速因子: 出口同比增速",
     "positive"),
    ("alt_macro_imports", "alternative", "compute_alt_macro_imports",
     "进口增速因子: 进口同比增速",
     "positive"),
    ("alt_macro_retail", "alternative", "compute_alt_macro_retail",
     "社消零售因子: 社会消费品零售总额同比增速",
     "positive"),
    ("alt_macro_real_estate", "alternative", "compute_alt_macro_real_estate",
     "房地产因子: 房地产开发投资/销售",
     "positive"),
    ("alt_macro_traffic", "alternative", "compute_alt_macro_traffic",
     "客货运量因子: 社会客货运量合计",
     "positive"),
    ("alt_macro_shibor", "alternative", "compute_alt_macro_shibor",
     "Shibor 利率因子: 全期限 Shibor 利率水平",
     "positive"),
    ("alt_macro_lpr", "alternative", "compute_alt_macro_lpr",
     "LPR 因子: 贷款市场报价利率",
     "positive"),
    ("alt_macro_money_supply", "alternative", "compute_alt_macro_money_supply",
     "货币供应量因子: 货币供应量/广义货币",
     "positive"),
    ("alt_macro_bank_financing", "alternative", "compute_alt_macro_bank_financing",
     "银行融资因子: 银行融资/信贷投放",
     "positive"),
    ("alt_macro_industrial", "alternative", "compute_alt_macro_industrial",
     "工业增加值因子: 工业增加值同比增速",
     "positive"),
    ("alt_macro_exports", "alternative", "compute_alt_macro_exports",
     "出口增速因子: 出口同比增速",
     "positive"),
    ("alt_macro_imports", "alternative", "compute_alt_macro_imports",
     "进口增速因子: 进口同比增速",
     "positive"),
    ("alt_macro_retail", "alternative", "compute_alt_macro_retail",
     "社消零售因子: 社会消费品零售总额同比增速",
     "positive"),
    ("alt_macro_real_estate", "alternative", "compute_alt_macro_real_estate",
     "房地产因子: 房地产开发投资/销售",
     "positive"),
    ("alt_macro_traffic", "alternative", "compute_alt_macro_traffic",
     "客货运量因子: 社会客货运量合计",
     "positive"),
]


def main():
    conn = sqlite3.connect(MARKET_DB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"Registering {len(ALTERNATIVE_FACTORS)} alternative factors...")
    registered = 0
    skipped = 0

    for name, category, compute_fn, source, direction in ALTERNATIVE_FACTORS:
        # Check if already exists
        row = conn.execute("SELECT name FROM factor_registry WHERE name=?", (name,)).fetchone()
        if row:
            print(f"  SKIP (exists): {name}")
            skipped += 1
            continue

        try:
            conn.execute("""
                INSERT INTO factor_registry
                (name, category, compute_fn, academic_source, status, status_reason, direction, created_at, updated_at)
                VALUES (?, ?, ?, ?, "evaluating", "New alternative factor from v582", ?, ?, ?)
            """, (name, category, compute_fn, source, direction, now, now))
            registered += 1
            print(f"  REGISTERED: {name} ({direction})")
        except Exception as e:
            print(f"  ERROR: {name} - {e}")

    conn.commit()
    print(f"\nDone: {registered} registered, {skipped} skipped")
    conn.close()


if __name__ == "__main__":
    main()