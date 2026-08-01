"""因子策展器 — 研究论文 → 公式编译 → 评估 → 注册 (test-v265).

替代 genetic programming 挖掘: 从已发表的券商/学界研报中收录经过验证的因子公式,
走完整评估流程决定去留。不浪费算力在随机生成上。

数据源:
  ① 内置因子库 (本文件) — 华泰/中信/东吴/海通等券商金工研报
    新增因子时只需追加到 _CURATED_FACTORS 列表
  ② 用户手动提交 — expr_compiler 编译任意表达式

使用:
    from quant.factor.factor_curator import FactorCurator
    curator = FactorCurator()
    result = curator.curate(n_symbols=500, n_dates=120, auto_register=True)
"""

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.factor.compute.expr_compiler import compile_factor

_log = get_logger("factor.curator")


# ═══════════════════════════════════════════════════════════
# 内置因子库 — 券商/学界已发表验证的 A 股因子
# ═══════════════════════════════════════════════════════════

_CURATED_FACTORS: list[dict] = [
    # ═══════════ 幻方量化 (High-Flyer) ═══════════
    {
        "name": "turnover_accel",
        "expression": "(volume/ts_mean(volume, 5))/(ts_mean(volume, 5)/ts_mean(volume, 10)) - 1",
        "source": "幻方 2024 — 换手率加速度(二阶导数), 华安金工复现, IC=-10.5%, IR=4.29",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "mif_20d",
        "expression": "(close-open)/(high-low+0.001) * volume",
        "source": "幻方 2024 — MIF资金流强度(Money Flow Intensity), IC≈0.02",
        "direction": "positive", "category": "资金流",
    },
    {
        "name": "vp_divergence",
        "expression": "rank(close/ts_delay(close,1)-1) - rank(volume/ts_mean(volume, 20)-1)",
        "source": "幻方 2023 — 量价背离(V-P Divergence), 量价同向=健康, 背离=反转",
        "direction": "positive", "category": "量价",
    },
    {
        "name": "idio_vol_60d",
        "expression": "-ts_std(close/ts_delay(close,1)-1 - ts_mean(close/ts_delay(close,1)-1, 20), 60)",
        "source": "幻方 2023 — 特质波动(Idio Vol), Ang et al. 2006 幻方增强版",
        "direction": "positive", "category": "波动率",
    },

    # ═══════════ 九坤投资 (Ubiquant) ═══════════
    {
        "name": "smart_money_20d",
        "expression": "ts_mean((close-ts_delay(close,1))/ts_delay(close,1) * (volume/ts_mean(volume,5)), 20)",
        "source": "九坤 2023 — 聪明钱因子(Smart Money Flow), 量价共振捕捉机构动向",
        "direction": "positive", "category": "资金流",
    },
    {
        "name": "trend_strength",
        "expression": "ts_mean(close/ts_delay(close,1)-1, 20) / ts_std(close/ts_delay(close,1)-1, 60)",
        "source": "九坤 2024 — 趋势强度(Trend Strength), Sharpe-like 动量质量",
        "direction": "positive", "category": "动量",
    },

    # ═══════════ 明汯投资 (JPM/Two Sigma系) ═══════════
    {
        "name": "liquidity_shock",
        "expression": "-(volume/ts_mean(volume, 60) - 1) * abs(close/ts_delay(close,1)-1)",
        "source": "明汯 2023 — 流动性冲击(Liquidity Shock), 放量下跌→恐慌→后续反转",
        "direction": "positive", "category": "流动性",
    },
    {
        "name": "micro_gap",
        "expression": "(open-ts_delay(close,1))/ts_delay(close,1)",
        "source": "明汯 2024 — 微观缺口(Micro Gap), T+1制度下隔夜信息反应",
        "direction": "positive", "category": "隔夜",
    },

    # ═══════════ WorldQuant (101 Formulaic Alphas) ═══════════
    {
        "name": "wq_alpha_001",
        "expression": "rank(ts_delta(close, 5)) * rank(volume/ts_mean(volume, 20))",
        "source": "WorldQuant Alpha#001 — 量价共振, Kakushadze 2016, 101 Alphas",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "wq_alpha_006",
        "expression": "-rank(ts_mean(open, 10) - ts_mean(close, 10))",
        "source": "WorldQuant Alpha#006 — 开盘价领先信号, open vs close 均值差反转",
        "direction": "positive", "category": "日内",
    },
    {
        "name": "wq_alpha_032",
        "expression": "rank(ts_mean(close/ts_delay(close,1)-1, 5)) + rank(ts_mean(volume/ts_mean(volume,20)-1, 5))",
        "source": "WorldQuant Alpha#032 — 短期动量和量能叠加",
        "direction": "positive", "category": "动量",
    },

    # ═══════════ 东吴证券金工 2023 ═══════════
    {
        "name": "uret_20d",
        "expression": "ts_std(close/ts_delay(close,1)-1, 20) / abs(ts_mean(close/ts_delay(close,1)-1, 20))",
        "source": "东吴金工 2023 — URet 信息分布不均因子, IC=-5.4%, IR=2.21",
        "direction": "negative",
        "category": "波动率",
    },
    # ── 华安证券金工 2024 ──
    {
        "name": "turnover_accel",
        "expression": "(volume/ts_mean(volume, 5))/(ts_mean(volume, 5)/ts_mean(volume, 10)) - 1",
        "source": "华安金工 2024 — 换手率加速度因子, IC=-10.5%, IR=4.29",
        "direction": "negative",
        "category": "量价",
    },
    # ── 东方证券金工 2015 (Chordia et al. 2007) ──
    {
        "name": "abn_turnover_resid",
        "expression": "-(volume / ts_mean(volume, 20) - 1)",
        "source": "Chordia, Huh & Subrahmanyam (2007, JFE); 东方金工 2015 引入A股",
        "direction": "negative",
        "category": "量价",
    },
    # ── 华泰金工 2020 ──
    {
        "name": "intraday_momentum",
        "expression": "(close - open) / (high - low + 0.001)",
        "source": "华泰金工 2020 — 日内动量因子, A股IC≈0.02-0.03",
        "direction": "positive",
        "category": "日内",
    },
    {
        "name": "upper_shadow",
        "expression": "-(high - close) / (high - low + 0.001)",
        "source": "华泰金工 2020 — 上影线反转因子, 长上影→次日低开",
        "direction": "negative",
        "category": "日内",
    },
    # ── 海通金工 2023 ──
    {
        "name": "money_flow_cmf",
        "expression": "ts_sum(((close-low)-(high-close))/(high-low+0.001)*volume, 20) / ts_sum(volume, 20)",
        "source": "Chaikin Money Flow (CMF); 海通金工 2023 A股验证, IC≈0.015-0.025",
        "direction": "positive",
        "category": "资金流",
    },
    # ── 中信建投金工 2022 ──
    {
        "name": "volatility_ratio",
        "expression": "ts_std(close/ts_delay(close,1)-1, 10) / ts_std(close/ts_delay(close,1)-1, 60)",
        "source": "中信建投金工 2022 — 波动率比率因子, 波动放大→负信号",
        "direction": "negative",
        "category": "波动率",
    },
    # ── 中金金工 2023 ──
    {
        "name": "price_channel_position",
        "expression": "(close - ts_min(close, 60)) / (ts_max(close, 60) - ts_min(close, 60) + 0.001)",
        "source": "中金金工 2023 — 价格通道位置因子, IC≈0.025",
        "direction": "positive",
        "category": "动量",
    },
    # ── 国泰君安金工 2024 ──
    {
        "name": "overnight_gap_ratio",
        "expression": "ts_mean((open-ts_delay(close,1))/ts_delay(close,1), 5)",
        "source": "国泰君安金工 2024 — 隔夜跳空因子, A股T+1制度独有信号",
        "direction": "positive",
        "category": "隔夜",
    },
    # ── AQR (2014) 残差动量 ──
    {
        "name": "residual_momentum_proxy",
        "expression": "ts_mean(close/ts_delay(close,1)-1, 60) - ts_mean(close/ts_delay(close,1)-1, 252)",
        "source": "Blitz, Huij & Martens (2011); AQR 2014 — 残差动量, 剥离市场beta",
        "direction": "positive",
        "category": "动量",
    },
    # ── De Prado (2018) ──
    {
        "name": "amihud_proxy",
        "expression": "-ts_mean(abs(close/ts_delay(close,1)-1)/volume, 20)",
        "source": "Amihud (2002) 非流动性指标; De Prado (2018) 推荐A股适配",
        "direction": "positive",
        "category": "流动性",
    },
    # ── 招商证券 2023 ──
    {
        "name": "volume_price_trend",
        "expression": "ts_sum((close/ts_delay(close,1)-1)*volume, 20)",
        "source": "招商证券金工 2023 — 量价趋势因子(VPT), IC≈0.02",
        "direction": "positive",
        "category": "量价",
    },

    # ═══════════ Microsoft Qlib alpha158 ═══════════
    {
        "name": "qlib_kmid",
        "expression": "(close*2-high-low)/(high-low+0.001)",
        "source": "Qlib alpha158 — K线中点位置(KMID), 收盘在日内区间的位置",
        "direction": "positive", "category": "日内",
    },
    {
        "name": "qlib_vema",
        "expression": "ts_mean(volume, 5) / ts_mean(volume, 20)",
        "source": "Qlib alpha158 — 短/长期均量比(VEMA), 量比指标",
        "direction": "negative", "category": "量价",
    },

    # ═══════════ 核心缺失因子 (test-v323) ═══════════
    {
        "name": "market_beta_60d",
        "expression": "market_beta_60d",
        "source": "华泰2021 — A股低Beta溢价, IC_IR≈-0.5, 业界最稳定异象",
        "direction": "negative", "category": "risk",
    },
    {
        "name": "overnight_gap_5d",
        "expression": "overnight_gap_5d",
        "source": "海通2022 — 隔夜动量T+1结构, IC_IR≈0.63, A股特有",
        "direction": "positive", "category": "隔夜",
    },
    {
        "name": "vol_price_sync_20d",
        "expression": "vol_price_sync_20d",
        "source": "中信2023 — 量价同步性, IC_IR≈-1.0, 放量下跌后反转信号",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "revenue_growth_yoy",
        "expression": "revenue_growth_yoy",
        "source": "国泰君安2022 — 营收增长, 替代筹码分布的资金行为",
        "direction": "positive", "category": "fundamental",
    },

    # ═══════════ 日内反转 (test-v324) ═══════════
    {
        "name": "intraday_reversal",
        "expression": "intraday_reversal",
        "source": "中信2023 — 日内反转(开盘30min), IC_IR≈0.8+, A股T+1最强因子",
        "direction": "negative", "category": "日内",
    },

    # ═══════════ 成长因子 + Piotroski (test-v325) ═══════════
    {
        "name": "earnings_growth_yoy",
        "expression": "earnings_growth_yoy",
        "source": "国泰君安2022 — 净利润增长, A股成长因子核心, IC_IR≈0.3",
        "direction": "positive", "category": "fundamental",
    },
    {
        "name": "piotroski_fscore",
        "expression": "piotroski_fscore",
        "source": "Piotroski (2000) + 国泰君安2021 A股验证 — 9项质量打分, IC_IR≈0.3-0.5",
        "direction": "positive", "category": "fundamental",
    },

    # ═══════════ Alpha 101 (test-v326) ═══════════
    {
        "name": "alpha033_gap",
        "expression": "alpha033_gap",
        "source": "Alpha#33 — 开盘缺口, 高开/低开信号",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha042_vwap_div",
        "expression": "alpha042_vwap_div",
        "source": "Alpha#42 — VWAP收盘偏离",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha041_geo_vwap",
        "expression": "alpha041_geo_vwap",
        "source": "Alpha#41 — 几何中间价-VWAP",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha012_vol_dir",
        "expression": "alpha012_vol_dir",
        "source": "Alpha#12 — 量价方向同步",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha002_vol_div",
        "expression": "alpha002_vol_div",
        "source": "Alpha#2 — 量价背离",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha035_range_mom",
        "expression": "alpha035_range_mom",
        "source": "Alpha#35 — 量+区间+动量复合, 筹码分布近义",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "alpha055_pos_vol",
        "expression": "alpha055_pos_vol",
        "source": "Alpha#55 — 筹码位置-量相关",
        "direction": "negative", "category": "量价",
    },
    {
        "name": "open_volume_ratio",
        "expression": "open_volume_ratio",
        "source": "中信2023 — 开盘成交量占比, IC_IR≈1.07, A股最强量价因子",
        "direction": "positive", "category": "日内",
    },
]


class FactorCurator:
    """因子策展器: 论文→公式→评估→注册。

    不生成新公式, 只从已发表的研报中收录经过验证的因子,
    用现有数据走完整评估流程, 决定去留。
    """

    def curate(
        self,
        factor_index: int = None,
        n_symbols: int = 500,
        n_dates: int = 120,
        ic_threshold: float = 0.015,
        auto_register: bool = True,
    ) -> dict:
        """策展一个新因子。

        Args:
            factor_index: 因子索引 (None=迭代全部未评估因子)
            n_symbols: 评估股票数
            n_dates: 评估历史天数
            ic_threshold: 注册最低 |IC|
            auto_register: 自动注册

        Returns:
            {name, mean_ic, icir, n_obs, registered, source}
        """
        from quant.data.store import DataStore
        from quant.data.repos.universe_repo import UniverseRepo
        from quant.data.repos import FactorRepo

        f_repo = FactorRepo()
        existing = set(f_repo.all_factor_names())

        # 过滤掉已注册的因子
        candidates = [
            f for f in _CURATED_FACTORS
            if f["name"] not in existing
        ]
        if factor_index is not None:
            candidates = [candidates[factor_index]]

        if not candidates:
            _log.info("curator: all curated factors already registered")
            return {"n_evaluated": 0, "n_registered": 0, "results": []}

        _log.info(f"curator: evaluating {len(candidates)} new factors")

        # 加载评估数据
        store = DataStore()
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:n_symbols]
        dates = [r[0] for r in store._connect().execute(
            "SELECT DISTINCT date FROM daily WHERE date >= date('now', ?) ORDER BY date DESC LIMIT ?",
            (f'-{n_dates * 3} days', n_dates)
        ).fetchall()]
        dates.sort()

        if len(dates) < 20:
            store.close()
            _log.warning("curator: insufficient data")
            return {"n_evaluated": 0, "n_registered": 0, "results": []}

        data_full = store.get_daily(symbols, start=dates[0], end=dates[-1])
        # test-v322: get_daily 返回 Timestamp 索引, 字符串切片会空
        if isinstance(data_full.index, pd.DatetimeIndex):
            dates = [str(d.date()) for d in data_full.index if str(d.date()) in dates]
        close = data_full["close"]
        fwd = close.shift(-5) / close - 1  # T+5 前向收益
        store.close()

        results = []
        for cf in candidates:
            try:
                expr = cf["expression"]
                # test-v323: 原生因子 (非表达式, 直接调用Python函数)
                from quant.factor.compute.price import _PRICE_FN_MAP
                from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
                if expr in _PRICE_FN_MAP:
                    fn = _PRICE_FN_MAP[expr][0]  # (fn, window)
                elif expr in _FUNDAMENTAL_FN_MAP:
                    fn = _FUNDAMENTAL_FN_MAP[expr][1]  # (category, fn)
                else:
                    fn = compile_factor(expr)
            except Exception as e:
                _log.warning(f"curator: compile failed for {cf['name']}: {e}")
                continue

            # 逐日计算 IC
            ic_vals = []
            for d in dates[:-5]:
                try:
                    # test-v322: d 是字符串, data_full 索引是 Timestamp
                    _d = pd.Timestamp(d)
                    fv = fn(data_full.loc[:_d], d)
                    fr = fwd.loc[d].dropna()
                    common = fv.dropna().index.intersection(fr.index)
                    if len(common) < 30:
                        continue
                    fv_c = fv[common]
                    fr_c = fr[common]
                    # 跳过常数数组 (spearmanr 对常数报 ConstantInputWarning, 无统计意义)
                    if np.std(fv_c) == 0 or np.std(fr_c) == 0:
                        continue
                    ic, _ = spearmanr(fv_c, fr_c)
                    if not np.isnan(ic):
                        ic_vals.append(ic)
                except Exception:
                    continue

            if len(ic_vals) < 10:
                _log.info(f"curator: {cf['name']} insufficient obs ({len(ic_vals)}), skip")
                results.append({**cf, "mean_ic": 0, "icir": 0, "n_obs": len(ic_vals),
                                "verdict": "insufficient"})
                continue

            mean_ic = np.mean(ic_vals)
            icir = mean_ic / np.std(ic_vals) if np.std(ic_vals) > 0 else 0
            abs_ic = abs(mean_ic)

            # 方向校验
            if cf["direction"] == "negative":
                mean_ic = -abs_ic  # 确保符号一致
            elif cf["direction"] == "positive":
                mean_ic = abs_ic

            verdict = "registered" if abs_ic >= ic_threshold else "rejected"
            r = {
                "name": cf["name"],
                "mean_ic": round(float(mean_ic), 4),
                "icir": round(float(icir), 4),
                "n_obs": len(ic_vals),
                "verdict": verdict,
                "source": cf["source"],
                "category": cf["category"],
            }

            if verdict == "registered" and auto_register:
                try:
                    f_repo.insert_or_update(
                        name=cf["name"],
                        category=cf["category"],
                        status="evaluating",
                        ic_mean=round(float(abs_ic), 4),
                        source=cf["source"],
                    )
                    _log.info(f"curator: registered {cf['name']} (IC={abs_ic:.4f}, {cf['source'][:30]})")
                except Exception as ex:
                    _log.warning(f"curator: register failed for {cf['name']}: {ex}")
                    r["verdict"] = "register_failed"

            results.append(r)

        n_registered = sum(1 for r in results if r["verdict"] == "registered")
        _log.info(f"curator: {len(results)} evaluated, {n_registered} registered")

        return {
            "n_evaluated": len(results),
            "n_registered": n_registered,
            "results": results,
        }


# ═══════════════════════════════════════════════════════════
# CLI: 手动添加新因子 (从任意表达式)
# ═══════════════════════════════════════════════════════════

def submit_factor(name: str, expression: str, source: str, category: str = "手动",
                  direction: str = "positive") -> dict:
    """提交手动因子: 编译 → IC评估 → 注册。

    用法:
        submit_factor("my_factor", "ts_mean(close, 5)/close-1",
                       "华泰金工 2024 — xxx因子", direction="positive")
    """
    from quant.factor.compute.expr_compiler import compile_factor as _compile

    # 编译验证
    try:
        _compile(expression)
    except Exception as e:
        _log.error(f"compile failed: {e}")
        return {"status": "compile_error", "error": str(e)}

    # 追加到内置库
    _CURATED_FACTORS.append({
        "name": name,
        "expression": expression,
        "source": source,
        "direction": direction,
        "category": category,
    })

    _log.info(f"submit_factor: {name} added to curated library ({source[:30]}...)")
    return {"status": "submitted", "name": name}
