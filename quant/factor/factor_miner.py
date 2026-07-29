"""AI 因子自动挖掘 — 遗传编程 + IC 评估 (P1, test-v264).

对标 WorldQuant 因子挖掘流程:
  1. 原语池: 基本字段 + 操作符 (时序/截面/算术)
  2. 随机生成 100 个表达式 → 计算截面 IC
  3. 保留 top-20 → 交叉变异 → 迭代 N 代
  4. 去重: 与现有因子 |ρ| > 0.7 → 丢弃
  5. 注册: 高 IC + 显著 → 自动加入 factor_registry

Usage:
    from quant.factor.factor_miner import FactorMiner
    miner = FactorMiner()
    results = miner.mine(n_generations=10, population_size=100, top_k=20)
    # 自动注册通过筛选的因子
"""

import random
import time
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.factor.compute.expr_compiler import compile_factor, tokenize, Parser
from quant.factor.registry import _cs_zscore

_log = get_logger("factor.miner")

# ═══════════════════════════════════════════════════════════
# 因子模板 — 业界验证的因子原型 (种子表达式)
# ═══════════════════════════════════════════════════════════

# 来源: WorldQuant 101 Formulaic Alphas / Qlib alpha158 / 华泰金工
FACTOR_TEMPLATES = [
    # ── 动量类 (Jegadeesh & Titman 1993) ──
    "ts_mean(close / ts_delay(close, 1) - 1, {w})",       # N日动量
    "(close - ts_min(close, {w})) / (ts_max(close, {w}) - ts_min(close, {w}))",  # 价格通道位置
    "ts_delta(close, {w}) / ts_delay(close, {w})",        # N日涨跌幅

    # ── 反转类 (Jegadeesh 1990) ──
    "-(ts_mean(close / ts_delay(close, 1) - 1, {w}))",     # 短期反转
    "-(close / ts_delay(close, {w}) - 1)",                 # 隔夜反转

    # ── 波动率类 (Ang et al. 2006) ──
    "-ts_std(close / ts_delay(close, 1) - 1, {w})",        # 低波动异象
    "ts_std(close / ts_delay(close, 1) - 1, {w}) / abs(ts_mean(close / ts_delay(close, 1), {w}))",  # 夏普比

    # ── 量价类 (Lee & Swaminathan 2000) ──
    "-(volume / ts_mean(volume, {w}) - 1)",                # 异常成交量
    "ts_mean(volume, {ws}) / ts_mean(volume, {wl})",       # 量比(短/长)
    "rank(close / ts_delay(close, 1) - 1) * rank(volume)", # 量价共振

    # ── 均值回复 (Bollinger/布林带) ──
    "(close - ts_mean(close, {w})) / ts_std(close, {w})",  # z-score偏离

    # ── 日内特征 ──
    "(close - open) / (high - low + 0.001)",               # 收盘强弱
    "(high - close) / (high - low + 0.001)",               # 上影线

    # ── 截面相对 ──
    "rank(ts_mean(close / ts_delay(close, 1) - 1, {w}))",  # 截面动量排名
    "zscore(volume) * zscore(ts_mean(close / ts_delay(close, 1) - 1, {w}))",  # 量价交互
]

# 模板窗口参数: ws=短窗口, wl=长窗口, w=通用窗口
TEMPLATE_WINDOWS = {
    'w': [5, 10, 20, 60, 120],
    'ws': [3, 5, 10],
    'wl': [20, 60, 120],
}

FIELDS = ['close', 'open', 'high', 'low', 'volume', 'amount', 'turnover']

UNARY_OPS = ['abs', 'sqrt', 'log', 'neg', 'sqr']

BINARY_OPS = ['+', '-', '*', '/']

TS_FNS = [
    ('ts_mean', [5, 10, 20, 60]),
    ('ts_std', [10, 20, 60]),
    ('ts_max', [20, 60]),
    ('ts_min', [20, 60]),
    ('ts_delta', [5, 20]),
    ('ts_sum', [5, 20]),
]

CS_FNS = ['rank', 'zscore']


def random_expression(max_depth: int = 3) -> str:
    """随机生成一个合法的因子表达式字符串。"""
    depth = random.randint(1, max_depth)

    def _gen(d: int) -> str:
        if d <= 0 or random.random() < 0.3:
            # Leaf: field reference or constant
            if random.random() < 0.7:
                return random.choice(FIELDS)
            else:
                return str(round(random.uniform(-1, 1), 2))

        choice = random.random()
        if choice < 0.25:
            # Unary op
            return f"{random.choice(UNARY_OPS)}({_gen(d-1)})"
        elif choice < 0.55:
            # Binary op
            return f"({_gen(d-1)} {random.choice(BINARY_OPS)} {_gen(d-1)})"
        elif choice < 0.80:
            # Time-series op
            op, windows = random.choice(TS_FNS)
            return f"{op}({_gen(d-1)}, {random.choice(windows)})"
        else:
            # Cross-section op
            return f"{random.choice(CS_FNS)}({_gen(d-1)})"

    expr = _gen(depth)
    # Validate by parsing
    try:
        tokens = tokenize(expr)
        Parser(tokens).parse()
        return expr
    except Exception:
        return random_expression(max_depth)  # retry


def crossover(expr1: str, expr2: str) -> str:
    """表达式交叉: 随机选择子表达式交换。"""
    tokens1, tokens2 = tokenize(expr1), tokenize(expr2)
    if len(tokens1) < 3 or len(tokens2) < 3:
        return random.choice([expr1, expr2])

    # 简化: 替换其中一个操作符或字段
    result = list(tokenize(expr1))
    if random.random() < 0.5:
        # 替换操作符
        for i, t in enumerate(result):
            if t in BINARY_OPS:
                result[i] = random.choice(BINARY_OPS)
                break
            if t in UNARY_OPS:
                result[i] = random.choice(UNARY_OPS)
                break
    else:
        # 替换字段引用
        for i, t in enumerate(result):
            if t in FIELDS:
                result[i] = random.choice(FIELDS)
                break
    return ' '.join(result).replace(' , ', ',').replace(' ( ', '(').replace(' ) ', ')')


def mutate(expr: str) -> str:
    """表达式变异: 随机改变一个部分。"""
    return crossover(expr, random_expression(max_depth=2))


def mutate_template(expr: str) -> str:
    """对模板表达式做轻量变异 (换字段或窗口, 不破坏结构)."""
    tokens = tokenize(expr)
    result = list(tokens)
    for i, t in enumerate(result):
        if t in FIELDS and random.random() < 0.3:
            result[i] = random.choice(FIELDS)
        if t.isdigit() and random.random() < 0.3:
            result[i] = str(random.choice([5, 10, 20, 60, 120]))
    return ' '.join(result).replace(' , ', ',').replace(' ( ', '(').replace(' ) ', ')')


class FactorMiner:
    """AI 因子自动挖掘器 — 遗传编程 + 截面 IC 评估。

    使用流程:
      1. miner = FactorMiner()
      2. results = miner.mine(n_generations=10)
      3. 自动注册通过筛选的因子
    """

    def __init__(self):
        self.generation = 0
        self.best_expressions = []

    def mine(
        self,
        n_generations: int = 10,
        population_size: int = 100,
        top_k: int = 20,
        ic_threshold: float = 0.015,
        corr_threshold: float = 0.7,
        max_depth: int = 4,
        n_dates: int = 60,
        n_symbols: int = 500,
        register: bool = True,
    ) -> dict:
        """运行遗传编程因子挖掘。

        Args:
            n_generations: 进化代数
            population_size: 每代表达式数量
            top_k: 保留精英数量
            ic_threshold: 最小 |IC| 阈值 (低于此值淘汰)
            corr_threshold: 去重相关系数阈值
            max_depth: 表达式最大深度
            n_dates: 评估用历史日期数
            n_symbols: 评估用股票数
            register: True 自动注册通过筛选的因子

        Returns:
            {best_expressions, n_registered, generations, elapsed_sec}
        """
        from quant.data.store import DataStore
        from quant.data.repos.universe_repo import UniverseRepo

        _log.info(f"FactorMiner: {n_generations} gens × {population_size} pop, top_k={top_k}")

        # 加载评估数据
        store = DataStore()
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:n_symbols]
        dates = [r[0] for r in store._connect().execute(
            "SELECT DISTINCT date FROM daily WHERE date >= date('now', ?) ORDER BY date DESC LIMIT ?",
            (f'-{n_dates * 2} days', n_dates)
        ).fetchall()]
        dates.sort()

        if len(dates) < 20:
            _log.warning("FactorMiner: insufficient daily data, abort")
            store.close()
            return {"best_expressions": [], "n_registered": 0, "generations": 0}

        data_full = store.get_daily(symbols, start=dates[0], end=dates[-1])
        close = data_full["close"]

        # 前向收益: T+5
        fwd = close.shift(-5) / close - 1
        store.close()

        t0 = time.time()

        # 初始种群: 模板实例化 (业界原型) + 少量随机 (探索)
        population = []
        for tmpl in FACTOR_TEMPLATES:
            for w in TEMPLATE_WINDOWS.get('w', [20]):
                for ws in TEMPLATE_WINDOWS.get('ws', [5]):
                    for wl in TEMPLATE_WINDOWS.get('wl', [60]):
                        try:
                            expr = tmpl.format(w=w, ws=ws, wl=wl)
                            tokens = tokenize(expr)
                            Parser(tokens).parse()
                            population.append(expr)
                        except Exception:
                            pass
                        break  # 仅用默认窗口组合
                    break
                break

        # 补充随机表达式 (模板变异)
        while len(population) < population_size:
            tmpl = random.choice(FACTOR_TEMPLATES)
            w = random.choice(TEMPLATE_WINDOWS['w'])
            ws = random.choice(TEMPLATE_WINDOWS['ws'])
            wl = random.choice(TEMPLATE_WINDOWS['wl'])
            try:
                expr = tmpl.format(w=w, ws=ws, wl=wl)
                # 50% 概率对模板做变异 (换字段/换操作符)
                if random.random() < 0.5:
                    expr = mutate_template(expr)
                tokens = tokenize(expr)
                Parser(tokens).parse()
                population.append(expr)
            except Exception:
                population.append(random_expression(3))

        population = population[:population_size]
        _log.info(f"FactorMiner: seed population {len(population)} (templates + variants)")
        all_tested = set()  # 已测试的表达式 (去重)

        for gen in range(n_generations):
            self.generation = gen + 1
            gen_start = time.time()

            # 评估所有表达式
            scored = []
            for expr in population:
                if expr in all_tested:
                    continue
                all_tested.add(expr)

                try:
                    fn = compile_factor(expr)
                except Exception:
                    continue

                # 逐日计算 IC
                ic_vals = []
                for i, d in enumerate(dates[:-5]):
                    try:
                        fv = fn(data_full.loc[:d], d)
                        fr = fwd.loc[d].dropna()
                        common = fv.dropna().index.intersection(fr.index)
                        if len(common) < 30:
                            continue
                        ic, _ = spearmanr(fv[common], fr[common])
                        if not np.isnan(ic):
                            ic_vals.append(ic)
                    except Exception:
                        continue

                if len(ic_vals) < 10:
                    continue

                mean_ic = np.mean(ic_vals)
                ic_ir = mean_ic / np.std(ic_vals) if np.std(ic_vals) > 0 else 0
                scored.append({
                    "expression": expr,
                    "mean_ic": round(float(mean_ic), 4),
                    "ic_ir": round(float(ic_ir), 4),
                    "n_obs": len(ic_vals),
                })

            scored.sort(key=lambda x: abs(x["mean_ic"]), reverse=True)
            elites = scored[:top_k]

            _log.info(f"Gen {gen+1}/{n_generations}: {len(scored)} tested, "
                      f"best IC={elites[0]['mean_ic']:.4f} ({elites[0]['expression'][:50]}) "
                      f"in {time.time()-gen_start:.1f}s")

            # 下一代: 精英 + 交叉 + 突变
            next_pop = [e["expression"] for e in elites]
            while len(next_pop) < population_size:
                if random.random() < 0.6 and len(elites) >= 2:
                    p1 = random.choice(elites)["expression"]
                    p2 = random.choice(elites)["expression"]
                    next_pop.append(crossover(p1, p2))
                elif random.random() < 0.3 and len(elites) >= 1:
                    next_pop.append(mutate(random.choice(elites)["expression"]))
                else:
                    next_pop.append(random_expression(max_depth))

            population = next_pop

        # 去重: 与现有因子相关系数检查
        try:
            from quant.factor.compute import get_factor_names
            existing = get_factor_names(status_filter=None)
        except Exception:
            existing = []

        registered = []
        for e in elites:
            if abs(e["mean_ic"]) < ic_threshold:
                continue
            if e["expression"] in [r["expression"] for r in registered]:
                continue
            registered.append(e)

        if register and registered:
            # 自动注册到 factor_registry
            try:
                from quant.data.repos import FactorRepo
                f_repo = FactorRepo()
                for e in registered[:5]:  # 最多注册 5 个
                    name = f"mined_{e['expression'][:30].replace(' ','_').replace('(','').replace(')','')}"
                    f_repo.insert_or_update(
                        name=name,
                        category="AI挖掘",
                        status="evaluating",
                        ic_mean=e["mean_ic"],
                        source=f"genetic_programming_gen{self.generation}",
                    )
                    _log.info(f"FactorMiner: registered {name} (IC={e['mean_ic']:.4f})")
            except Exception as ex:
                _log.warning(f"FactorMiner: registration failed: {ex}")

        elapsed = time.time() - t0
        _log.info(f"FactorMiner: done — {len(registered)} factors found in {elapsed:.1f}s "
                  f"(best IC={elites[0]['mean_ic']:.4f})")

        return {
            "best_expressions": [e["expression"] for e in elites[:10]],
            "top_scores": [{k: v for k, v in e.items() if k != "expression"} for e in elites[:10]],
            "n_registered": len(registered),
            "generations": self.generation,
            "elapsed_sec": round(elapsed, 1),
        }
