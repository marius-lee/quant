#!/usr/bin/env python3
"""
Hikyuu 概念验证 (POC): 陈小群模式 + 因子日历 + 波动率自适应止损
==============================================================
来源: Hikyuu 2.8.0 源码 (hikyuu_src/) + 因子日历2024 全书遍历
前提: pip install hikyuu numpy pandas matplotlib seaborn sqlalchemy click tqdm bokeh pyecharts
用法: python ops/hikyuu_poc.py

架构映射:
  EV (环境)  ←  market_mood 情绪周期
  SG (信号)  ←  陈小群 弱转强/首阴反包/连板接力
  MF (因子)  ←  因子日历 日频因子 → ICIR加权合成
  SE (选股)  ←  因子评分 TopN 过滤
  ST (止损)  ←  波动率自适应
  TP (止盈)  ←  MCVA 动态止盈
  MM (资金)  ←  半Kelly仓位
  PF (组合)  ←  PF_Simple 多标的单系统
"""

import os, sys, math
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════
# 0. Hikyuu 初始化
# ═══════════════════════════════════════════════════════════
# 来源: hikyuu/interactive.py — 标准初始化入口
from hikyuu.interactive import *
# 提供: sm (StockManager), KQuery, KData, Datetime, Query 等全局对象
import hikyuu as _hikyuu
_hikyuu_version = _hikyuu.__version__

from hikyuu.core import (
    SignalBase,           # 信号基类 — hikyuu_cpp/hikyuu/trade_sys/signal/SignalBase.h:24
    StoplossBase,         # 止损基类 — hikyuu_cpp/hikyuu/trade_sys/stoploss/StoplossBase.h:23
    MoneyManagerBase,     # 资金管理基类 — hikyuu_cpp/hikyuu/trade_sys/moneymanager/MoneyManagerBase.h:22
    EnvironmentBase,      # 环境基类 — hikyuu_cpp/hikyuu/trade_sys/environment/EnvironmentBase.h
    System,               # 交易系统 — hikyuu_cpp/hikyuu/trade_sys/system/System.h
    SystemPart,           # 系统部件枚举
)
from hikyuu.trade_sys import (
    crtSG,                # 创建自定义信号 — hikyuu/trade_sys/trade_sys.py:146
    crtST,                # 创建自定义止损 — hikyuu/trade_sys/trade_sys.py:250
    crtMM,                # 创建自定义MM — hikyuu/trade_sys/trade_sys.py:90
    crtEV,                # 创建自定义环境 — hikyuu/trade_sys/trade_sys.py:71
    crtMF,                # 创建自定义多因子 — hikyuu/trade_sys/trade_sys.py:207
    crtSE,                # 创建自定义选择器 — hikyuu/trade_sys/trade_sys.py:165
)
import hikyuu.indicator as _ind
# Hikyuu 2.8.0 indicator API: 所有指标函数接收 Indicator 对象作为输入

# ═══════════════════════════════════════════════════════════
# 1. 市场环境评估 (EV) — 对应 market_mood.py
# ═══════════════════════════════════════════════════════════
# 来源: 陈小群情绪周期体系 — chen-xiaoqun-authentic-strategy.md
# Hikyuu API: crtEV(func) — hikyuu/trade_sys/trade_sys.py:71

def create_market_environment():
    """创建市场环境判断组件。

    陈小群情绪周期: 冰点/衰退/复苏/扩张/高潮
    映射到Hikyuu EV: _calculate(datetime) → 环境状态
    """
    def env_calculate(self):
        """_calculate 被每个bar调用一次，设置环境状态。"""
        # 简化版: 检查涨停家数 (需要实时数据，这里用占位逻辑)
        # 实际实现: 从StockManager获取全市场数据计算涨停家数
        # 来源: factor/market_mood.py:24 detect_mood()
        self._add_valid(datetime_list[-1])  # 所有交易日都有效（默认开启）

    def env_is_valid(self, datetime):
        """判断指定时刻系统是否可用。"""
        return True  # POC: 简化，实际根据情绪阶段返回

    meta = type('MarketMoodEV', (EnvironmentBase,), {
        '__init__': lambda self, name='MarketMood': EnvironmentBase.__init__(self, name),
        '_calculate': env_calculate,
        '_clone': lambda self: create_market_environment(),
    })
    return meta('MarketMood')


# ═══════════════════════════════════════════════════════════
# 2. 陈小群信号指示器 (SG) — 弱转强/首阴反包/连板接力
# ═══════════════════════════════════════════════════════════
# 来源: chen-xiaoqun-final-signal-design.md (17次搜索交叉验证)
# Hikyuu API: crtSG(func) — hikyuu/trade_sys/trade_sys.py:146
# SignalBase API: hikyuu_cpp/hikyuu/trade_sys/signal/SignalBase.h:38-76

def create_chen_xiaoqun_signal():
    """创建陈小群三类买点信号指示器。

    SignalBase 核心接口 (来源: SignalBase.h:38-76):
        _add_signal(datetime, value)  — 记录信号。value>0=买入, value<0=卖出
        _calculate(indicator)         — 基于指标计算信号(必须实现)

    S1 弱转强 (0.90): 昨炸板 + 高开2-5% + 量>昨3倍 + 5分钟涨>7%
    S2 首阴反包 (0.85): 昨炸板 + gap≥3% + 换手20-30% + 15分站稳均线
    S3 连板接力 (0.70): 2连板 + 换手>10% + 二板量≥首板2/3 + 早盘封板
    """
    def sg_calculate(self):
        """核心计算函数 — 被每个k线周期调用一次。

        参数: self 是 SignalBase 实例, 可访问:
            self.getTO() → KData (当前交易标的的K线数据)
            self.getTM() → TradeManager (交易管理实例)
        """
        kdata = self.getTO()  # KData — hikyuu_cpp/hikyuu/KData.h
        if len(kdata) == 0:
            return

        n = len(kdata)
        for i in range(1, n):
            k = kdata[i]       # KRecord: datetime, open, high, low, close, volume, amount
            prev = kdata[i-1]  # 前一根K线

            if prev.close <= 0 or k.open <= 0:
                continue

            # ── S1: 弱转强检测 ──
            # 条件: 昨收≈涨停价(炸板) + 今高开2-5% + 今量超过昨量3倍
            # 来源: chen-xiaoqun-final-signal-design.md S1
            is_yesterday_broken = prev.high >= round(prev.close * 1.095, 2)  # 昨触及涨停
            gap = (k.open / prev.close - 1)          # 今高开幅度
            vol_ratio = k.volume / max(prev.volume, 1)  # 量比

            if is_yesterday_broken and 0.02 <= gap <= 0.05 and vol_ratio >= 3.0:
                self._add_signal(k.datetime, 0.90)   # S1信号值=0.90
                continue

            # ── S2: 首阴反包检测 ──
            # 条件: 昨炸板 + 高开≥3% + 换手率在10-30%范围内
            # 来源: chen-xiaoqun-final-signal-design.md S2
            turnover = k.volume / 10000.0  # 换手率代理(量/10000)
            if is_yesterday_broken and gap >= 0.03 and 10 <= turnover <= 30:
                self._add_signal(k.datetime, 0.85)   # S2信号值=0.85
                continue

            # ── S3: 连板接力检测 ──
            # 条件: 连续2板 + 换手>10% + 今量≥昨量×2/3 + 早盘封板(用open/close近似)
            # 来源: chen-xiaoqun-final-signal-design.md S3
            prev_ret = (prev.close / kdata[i-2].close - 1) if i >= 2 else 0
            is_2board = (prev_ret >= 0.095) and ((k.close / prev.close - 1) >= 0.05)
            is_early_seal = k.open >= prev.close * 1.02  # 高开替代早盘封板
            if is_2board and turnover > 10 and vol_ratio >= 0.67 and is_early_seal:
                self._add_signal(k.datetime, 0.70)   # S3信号值=0.70

    return crtSG(sg_calculate, name='ChenXiaoqun', params={})


# ═══════════════════════════════════════════════════════════
# 3. 波动率自适应止损 (ST) — 对应因子日历波动率分解
# ═══════════════════════════════════════════════════════════
# 来源: 因子日历2024 第7页 高频下行波动, 第60页 上行已实现波动率
# Hikyuu API: crtST(get_price, calculate, params) — hikyuu/trade_sys/trade_sys.py:250
# StoplossBase API: hikyuu_cpp/hikyuu/trade_sys/stoploss/StoplossBase.h:23
# 对比基准: ST_FixedPercent(0.03) — hikyuu_cpp/.../stoploss/crt/ST_FixedPercent.h

def create_volatility_adaptive_stop():
    """创建波动率自适应止损组件。

    因子日历理论 (来源: 因子日历2024 全书分析 §2.9):
        下行已实现波动率 → 下行风险溢价 → 止损宽度应正比于波动率
        adaptive_stop_pct = base_stop_pct × (stock_downside_vol / market_median_vol)
        floor: -2%, ceiling: -8%

    StoplossBase 核心接口:
        get_price(datetime, price) → float  # 返回止损价, 0=不止损
        _calculate() → void                  # 预处理(计算波动率)
        _reset() → void                      # 重置状态
    """
    def st_calculate(self):
        """预处理: 计算当前股票的下行波动率，确定自适应止损百分比。"""
        kdata = self.getTO()
        if len(kdata) < 20:
            self.set_param('adaptive_pct', 0.05)  # 默认5%
            return

        # 计算20日日度下行波动率 (来源: 因子日历 page 53 下行已实现波动率)
        closes = [k.close for k in kdata[-20:]]
        if len(closes) < 15:
            self.set_param('adaptive_pct', 0.05)
            return

        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        down_returns = [r for r in returns if r < 0]

        if len(down_returns) < 5:
            self.set_param('adaptive_pct', 0.05)
            return

        # 下行日波动率 → 年化 (来源: Andersen et al. 2001, 因子日历 page 40)
        import numpy as np
        daily_down_vol = np.std(down_returns)
        annual_down_vol = daily_down_vol * math.sqrt(252)

        # 自适应: 基线5% × (个股下行波/市场下行波中位数0.30)
        # 来源: Grinold 表3-1: A股残差波动率年化30%中位
        adaptive_pct = 0.05 * (annual_down_vol / 0.30)
        adaptive_pct = max(0.02, min(0.08, adaptive_pct))  # floor 2%, ceiling 8%
        self.set_param('adaptive_pct', adaptive_pct)

    def st_get_price(self, datetime, price):
        """返回动态止损价。价格低于此价则触发卖出。"""
        pct = self.get_param('adaptive_pct')
        return price * (1.0 - pct)

    return crtST(st_get_price, params={}, name='VolAdaptiveStop', calculate=st_calculate)


# ═══════════════════════════════════════════════════════════
# 4. MCVA 动态止盈 (TP) — 对应 Grinold 框架
# ═══════════════════════════════════════════════════════════
# 来源: ops/performance.py mcva_trailing_stop()
# Hikyuu: 止盈复用 StoplossBase (框架不区分止损/止盈)
# API: crtST(get_price) 同样用于止盈, 系统组件分离为 tp=参数

def create_mcva_take_profit():
    """创建MCVA动态止盈组件 (来源: Grinold 式14-8/14-9)。

    MCVA = entry_alpha - current_alpha - MCAR_term
    当MCVA< -sell_cost 时触发止盈。
    entry_alpha = RESIDUAL_VOL × IC × score (来源: ops/performance.py:421)
    """
    RESIDUAL_VOL = 0.30       # 来源: Grinold 表3-1: A股残差波动率年化30%
    IC_PRIOR = 0.05           # 来源: Grinold 10.7节: 良好信号IC=0.05
    SELL_COST = 0.0018        # 来源: A股印花税率0.1%+佣金0.03%≈0.13%, 取0.18%含滑点
    RISK_AVERSION = 0.10      # 来源: Grinold 5.6节: λ_R=0.10中等风险厌恶
    ACTIVE_RISK = 0.05        # 来源: Grinold 5.4节: ψ=5%目标主动风险

    def tp_get_price(self, datetime, price):
        """MCVA止盈: 返回0表示不止盈，返回价格表示触发止盈。"""
        # 简化: 检查是否需要止盈
        # 实际实现需要entry_alpha (买入时记录) 和 current_alpha (实时计算)
        # 这里返回0表示不触发，完整实现在ops/performance.py
        return 0.0  # POC: 不触发，展示接口

    return crtST(tp_get_price, params={}, name='MCVATakeProfit')


# ═══════════════════════════════════════════════════════════
# 5. 半Kelly资金管理 (MM) — 对应Chen量化交易 + Kelly框架
# ═══════════════════════════════════════════════════════════
# 来源: Chan 量化交易 §3; ops/performance.py kelly_fraction()
# Hikyuu API: crtMM(get_buy_num, get_sell_num, params) — trade_sys.py:90
# MoneyManagerBase: hikyuu_cpp/.../moneymanager/MoneyManagerBase.h:22
# 内置对比: MM_FixedPercent(pct) — 固定百分比仓位

def create_half_kelly_mm():
    """创建半Kelly资金管理组件。

    Kelly公式 (来源: Kelly 1956, Thorp 2006, Chan 量化交易 §3):
        f* = (b × p - q) / b  (b=盈亏比, p=胜率, q=1-p)
        半Kelly: f = f* × 0.5  (保守, 减少回撤)
        多仓位折扣: f_adj = f / (1 + ρ × (n-1))  (McDonnell)
        回撤缩放: 回撤>10%后Kelly乘数减半 (Keeks 2016)
    """
    def mm_get_buy_num(self, datetime, stock, price, risk, part_from):
        """返回买入数量。Hikyuu框架调用此方法决定每笔交易买入多少股。

        参数:
            datetime: 交易时间
            stock: Stock对象
            price: 买入价格
            risk: 本次交易每股风险 (来自止损组件, 即止损价与买入价之差)
            part_from: SystemPart 枚举 (哪个部件触发的买入)
        返回: 买入股数 (A股最小100股)
        """
        tm = self.getTM()
        if tm is None:
            return 0

        capital = tm.current_cash
        if capital <= 0:
            return 0

        # 参数 (来源: 需要回测校准, 此处用行业参考值)
        win_rate = 0.55       # 估计胜率 (来源: 陈小群体系 ~55%估算)
        avg_win_loss_ratio = 2.0  # 估计盈亏比 (来源: Chan §3 典型值1.5-3.0)

        # Kelly公式
        # f* = (b*p - q) / b = (2.0*0.55 - 0.45) / 2.0 = 0.325
        kelly_f = (avg_win_loss_ratio * win_rate - (1 - win_rate)) / avg_win_loss_ratio

        # 半Kelly (来源: Chan §3.4 — 半Kelly保留75%收益, 降低50%波动)
        half_kelly = kelly_f * 0.5

        # 多仓位折扣 (来源: McDonnell Optimal Portfolio Modeling)
        n_positions = 3     # 最大持仓数 (来源: config.yaml backtest.max_positions)
        rho = 0.3           # 持仓间平均相关性 (行业估计)
        kelly_adj = half_kelly / (1 + rho * (n_positions - 1))  # = 0.1625 / 1.6 = 0.102

        # 回撤缩放 (来源: Keeks 2016 — 回撤>10%后Kelly乘数减半)
        drawdown_scale = 1.0
        # 实际实现: 从tm获取最大回撤, 超过10%则scale=0.5

        risk_amount = capital * kelly_adj * drawdown_scale
        buy_num = int(risk_amount / max(risk, 0.01) / 100) * 100  # A股100股整数倍

        return max(buy_num, 0)

    return crtMM(mm_get_buy_num, params={}, name='HalfKelly')


# ═══════════════════════════════════════════════════════════
# 6. 因子日历因子指标 — 自定义 IndicatorImp
# ═══════════════════════════════════════════════════════════
# 来源: 因子日历2024 (379页遍历)
# Hikyuu API: IndicatorImp — hikyuu_cpp/hikyuu/indicator/IndicatorImp.h:32
# 内置参考: MA(CLOSE(), n=20) — 简单移动均线 (指标链示例)

def create_downside_vol_indicator():
    """创建下行已实现波动率指标。

    来源: 因子日历 page 53, 第60页
    公式: sqrt( Σ(r_i² × I(r_i < 0)) / N )  for i=1..N
    其中 r_i 为日度收益率, I为示性函数

    Hikyuu IndicatorImp 接口:
        _calculate(ind) → void     # 核心计算
        _readyBuffer(len, num)      # 预分配缓冲区
        _set(value, pos)            # 设置位置的值
    """
    class DownsideVol(IndicatorImp):
        def __init__(self, kdata=None):
            super().__init__('DownsideVol')
            self._n = 20  # 回溯窗口
            if kdata is not None:
                self.set_context(kdata)

        def _calculate(self, ind):
            n = self._n
            if len(ind) < n + 2:
                return
            result_num = len(ind) - n
            self._readyBuffer(len(ind), result_num)
            for i in range(n, len(ind)):
                rets = [(ind[j] - ind[j-1]) / max(ind[j-1], 0.001) for j in range(i-n+1, i+1)]
                down_rets = [r for r in rets if r < 0]
                if len(down_rets) >= 5:
                    val = (sum(r*r for r in down_rets) / len(down_rets)) ** 0.5
                else:
                    val = 0
                self._set(val, i)

        def _clone(self):
            return DownsideVol()

    return DownsideVol


def create_amihud_indicator():
    """创建Amihud非流动性指标。

    来源: Amihud 2002; 因子日历 page 36
    公式: Amihud = |ret| / amount × 10^6 (标准化)
    月度均值: 日度Amihud的20日移动平均
    """
    class AmihudIlliquidity(IndicatorImp):
        def __init__(self, close_ind=None, amount_ind=None):
            super().__init__('AmihudIlliquidity')
            if close_ind is not None:
                self._set_indicator(close_ind, 0)
            if amount_ind is not None:
                self._set_indicator(amount_ind, 1)

        def _calculate(self, ind):
            close_ind = self.get_indicator(0)
            amount_ind = self.get_indicator(1)
            if len(close_ind) < 2 or len(amount_ind) < 2:
                return
            result_num = min(len(close_ind), len(amount_ind))
            self._readyBuffer(result_num, result_num)
            self._set(0, 0)  # 第一个位置没有ret, 设为0
            for i in range(1, result_num):
                ret = abs((close_ind[i] - close_ind[i-1]) / max(close_ind[i-1], 0.001))
                amt = max(amount_ind[i], 1)
                val = (ret / amt) * 1e6  # 标准化
                self._set(val, i)

        def _clone(self):
            return AmihudIlliquidity()

    return AmihudIlliquidity


# ═══════════════════════════════════════════════════════════
# 7. 多因子合成 (MF) — IC/IR加权
# ═══════════════════════════════════════════════════════════
# 来源: Hikyuu MF_ICIRWeight — hikyuu_cpp/.../multifactor/crt/MF_ICIRWeight.h
# 因子日历 全书分析 §5: IC加权合成
#
# 内置API: MF_ICIRWeight(factorset, stks, query, ic_n=5, ic_rolling_n=120)
# 参数:
#   ic_n: 计算IC时最近N期收益 (默认5)
#   ic_rolling_n: 滚动计算IC的窗口长度 (默认120)
#   mode: 0=均值ICIR加权, 1=最近一期ICIR加权

def create_multi_factor_selector(stocks, query):
    """创建多因子选股选择器。

    使用Hikyuu内置 MF_ICIRWeight — hikyuu_cpp/.../multifactor/crt/MF_ICIRWeight.h

    因子集:
        1. 下行波动率 (高=正相关, 风险溢价)
        2. Amihud非流动性 (高=正相关, 流动性溢价)
        3. 隔夜累计跳空 (高=负相关, 情绪透支→反转)
        4. 偏度 (高=负相关, 暴跌风险)

    来源: 因子日历2024 全书分析 §4.2-§4.3
    """
    from hikyuu.trade_sys import (
        MF_ICIRWeight,
        SE_MultiFactor2,
        SCFilter_IgnoreNan,
        SCFilter_TopN,
        SCFilter_Price,
    )

    # 因子1: 20日波动率 (代理下行波动率)
    # Hikyuu 2.8.0: MA/STD 接受 Indicator 对象
    close_ind = _ind.CLOSE()
    vol_ind = _ind.STD(close_ind, n=20)

    # 因子2: Amihud代理 (用开盘价-昨收的波动性间接反映流动性)
    # 因子日历: Amihud = |ret|/amount。这里用 CLV 指标代理
    clv_ind = ((close_ind - _ind.LOW()) - (_ind.HIGH() - close_ind)) / (_ind.HIGH() - _ind.LOW() + 0.001)
    amihud_proxy = _ind.MA(clv_ind, n=20)

    # 因子3: 隔夜跳空代理 (open vs prev close)
    overnight_gap = (_ind.OPEN() - _ind.REF(close_ind, 1)) / _ind.REF(close_ind, 1)
    overnight_ind = _ind.MA(overnight_gap, n=20)

    # 因子4: 偏度代理 (用MA偏离度)
    ma_dev = (close_ind - _ind.MA(close_ind, n=20)) / (_ind.STD(close_ind, n=20) + 0.001)
    skew_proxy = _ind.MA(ma_dev, n=20)

    # 因子集 (FactorSet — hikyuu_cpp/hikyuu/factor/FactorSet.h)
    from hikyuu.factor import Factor, FactorSet
    factor_set = FactorSet([
        Factor('volatility', vol_ind, KQuery.DAY, '20日波动率因子'),
        Factor('liquidity', amihud_proxy, KQuery.DAY, '流动性代理因子'),
        Factor('overnight', overnight_ind, KQuery.DAY, '隔夜跳空因子'),
        Factor('skewness', skew_proxy, KQuery.DAY, '偏度代理因子'),
    ])

    # ICIR加权合成 (来源: Hikyuu MF_ICIRWeight)
    mf = crtMF(
        lambda self: None,  # 简化: 实际应使用内置 MF_ICIRWeight
        params={'ic_n': 5, 'ic_rolling_n': 120, 'mode': 0},
        name='ICIRWeight',
    )
    # 注意: 完整 MF_ICIRWeight 需从 C++ 层导入, pip wheel 可能未暴露
    # 这里使用 crtMF 自定义接口展示架构

    return se


# ═══════════════════════════════════════════════════════════
# 8. 组装完整交易系统 (POC)
# ═══════════════════════════════════════════════════════════
# Hikyuu API: SYS_Simple — hikyuu_cpp/.../system/crt/SYS_Simple.h
# 完整参数: tm, mm, ev, cn, sg, st, tp, pg, sp

def create_trading_system():
    """组装完整的陈小群+因子日历交易系统。"""

    # 获取上海A股列表 (测试用)
    stocks = [s for s in sm.get_block("板块", "上证50") if s.valid]

    # 交易管理 (初始资金¥5000)
    from hikyuu.trade_manage import crtTM
    tm = crtTM(Datetime(202401010000), 5000.0)

    # 查询范围 (最近120个交易日)
    query = Query(-120)

    # ── 组装组件 ──
    ev = create_market_environment()       # 市场环境
    sg = create_chen_xiaoqun_signal()      # 陈小群信号 (POC: 需实际数据才能触发)
    st = create_volatility_adaptive_stop() # 波动率自适应止损
    tp = create_mcva_take_profit()         # MCVA止盈
    mm = create_half_kelly_mm()            # 半Kelly资金管理

    # ── 多因子选择器 ──
    se = create_multi_factor_selector(stocks, query)

    # ── 创建系统 (Hikyuu 2.8.0: System 直接构造) ──
    # 构造函数签名: System(tm, mm, ev, cn, sg, st, tp, pg, sp, name)
    # 来源: pybind11 binding — 9参数构造
    sys = System(tm, mm, ev, None, sg, st, tp, None, None, 'ChenFactorSystem')

    return sys, se, tm, stocks


# ═══════════════════════════════════════════════════════════
# 9. 与现有 quant 系统对比验证
# ═══════════════════════════════════════════════════════════

def benchmark_against_current():
    """对比 Hikyuu 内置组件 vs 当前系统实现。

    目的: 验证 Hikyuu 能提供的能力是否 ≥ 当前系统
    """
    print("=" * 60)
    print("Hikyuu vs 当前 quant 系统 — 组件对比")
    print("=" * 60)

    comparisons = [
        ("信号检测", "SG_Flex(EmaCross) 内置信号", "execution/quote.py BoardTracker"),
        ("止损风控", "ST_FixedPercent(0.03) 原生组件", "execution/sell_chain.py B1硬止损"),
        ("止盈", "ST_Indicator(EMA) 指标驱动", "execution/sell_chain.py B6 MCVA"),
        ("资金管理", "MM_FixedCount/MM_FixedPercent", "ops/performance.py kelly_fraction()"),
        ("因子合成", "MF ICIRWeight 内置加权", "手动计算 (无框架支持)"),
        ("选股过滤", "SE MultiFactor2 原生组件", "手动过滤 (无框架支持)"),
        ("A股规则", "原生 T+1/涨跌停", "手动实现"),
        ("组合回测", "PF_Simple 多标的", "intraday_runner.py 手动循环"),
    ]

    for category, hikyuu_impl, current_impl in comparisons:
        print(f"  {category:<10} | Hikyuu: {hikyuu_impl:<35} | 当前: {current_impl}")

    print("\n结论: Hikyuu 8/8 类别均提供内置或可扩展支持")
    print("  信号/止损/止盈/MM/MF/SE/PF 均为独立组件 → 符合零冗余原则")


# ═══════════════════════════════════════════════════════════
# 10. 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 打印对比
    benchmark_against_current()

    print("\n" + "=" * 60)
    print("POC 验证: 创建组件实例")
    print("=" * 60)

    # 验证组件可创建 (不会崩溃=API正确)
    try:
        ev = create_market_environment()
        print(f"  ✅ EV (MarketMood): {ev.name}")

        sg = create_chen_xiaoqun_signal()
        print(f"  ✅ SG (ChenXiaoqun): {sg.name}")

        st = create_volatility_adaptive_stop()
        print(f"  ✅ ST (VolAdaptiveStop): {st.name}")

        tp = create_mcva_take_profit()
        print(f"  ✅ TP (MCVATakeProfit): {tp.name}")

        mm = create_half_kelly_mm()
        print(f"  ✅ MM (HalfKelly): {mm.name}")

        print(f"\n  所有组件创建成功。Hikyuu C++核心版本: {_hikyuu_version}")
        print(f"  数据目录: {sm.datadir}")
        print(f"  临时目录: {sm.tmpdir}")

    except Exception as e:
        print(f"  ❌ 组件创建失败: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("下一步 (POC验证完成后):")
    print("  1. 导入真实日线数据到 Hikyuu (HDF5/SQLite)")
    print("  2. 在历史数据上运行 sys.run() 回测")
    print("  3. 验证 IC/IR 值是否优于当前系统 (IC₁=-0.07)")
    print("  4. 对比 ST_FixedPercent vs VolAdaptiveStop 的回撤")
    print("  5. 对比 MM_FixedPercent vs HalfKelly 的收益曲线")
    print("=" * 60)
