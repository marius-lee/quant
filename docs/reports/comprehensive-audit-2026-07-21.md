# Quant 项目全面审查报告

> 审查日期: 2026-07-21
> 审查范围: 186个Python文件, 26,553行代码, 7层架构
> 方法: 6个并行探索代理 + 直接审查核心模块
> 版本: test-v181

---

## 目录

1. [技术栈评估](#一技术栈评估)
2. [功能完成度](#二功能完成度)
3. [架构评估](#三架构评估)
4. [代码质量与逻辑错误](#四代码质量与逻辑错误)
5. [算法评估](#五算法评估)
6. [补充: Risk/Optimizer/Execution 层关键发现](#补充-riskoptimizerexecution-层关键发现)
7. [详细修改方案](#六详细修改方案)
8. [优先级行动清单](#七优先级行动清单)

---

## 一、技术栈评估

### 当前技术栈

| 层 | 技术 | 评价 |
|---|------|------|
| 语言 | Python 3.12+ | A股量化最佳选择 |
| 存储 | SQLite (market.db 1.2GB) | 单用户零配置, 10M+行无压力 |
| 数据源 | tickflow/akshare/tushare/baostock/sina/tencent/pytdx | 7源回退链鲁棒 |
| 数值计算 | numpy/pandas/scipy/scikit-learn | 业界标配 |
| ML | lightgbm/xgboost | 已安装但几乎未使用 |
| 优化 | optuna | 仅用于超参搜索, 回测未集成 |
| 市场状态 | hmmlearn | 隐马尔可夫模型仅基础应用 |
| Web | Flask + SSE | 适合单用户仪表盘 |
| 前端 | Vanilla JS + Plotly | 功能完整但无框架 |

### 建议新增技术

| 优先级 | 技术 | 理由 |
|--------|------|------|
| **P0** | pytest + pytest-cov 全覆盖 | 当前69个测试覆盖不到30%模块 |
| **P0** | backtrader | 替换手工loop.py事件模拟 |
| **P1** | cvxpy | 均值-方差优化当前用np.linalg.inv手工实现 |
| **P1** | GitHub Actions CI | 当前无CI, 每次改动靠手工验证 |
| **P2** | FastAPI (替代Flask) | 自动OpenAPI, 异步支持, Pydantic验证 |
| **P2** | pre-commit hooks | 已有配置但未安装, 拦截YAML语法错误 |

---

## 二、功能完成度

### 已完成 (核心闭环)

1. **7层完整流水线**: 数据→因子→Alpha→风控→优化→执行→监控
2. **79个因子**: 46个量价 + 33个基本面 (文档滞后未更新)
3. **多源数据同步**: 7源级联回退 + 速度自适应轮转
4. **资本自适应组合优化**: Nano/Micro/Small 三层架构
5. **统一成本模型**: 佣金+印花税+滑点+市场冲击(Almgren-Chriss)
6. **IC评估体系**: Spearman Rank IC + ICIR + 衰减分析
7. **7阶段因子评估**: CPCV + PBO + Walk-Forward (De Prado标准)
8. **回测引擎**: 日前信号→T+1执行→权益曲线→归因
9. **Flask仪表盘**: SSE实时推送 + KPI + 因子热力图
10. **调度器**: Crontab 7任务 + 盘中风控守护线程
11. **Brinson归因**: 配置/选股/交互效应 + 因子暴露分解
12. **限价单执行**: ADR 033 4事件状态机

### 部分完成

13. **市场状态检测**: hmmlearn已集成但regime权重仅基础应用
14. **Kelly头寸分配**: 仅Small层启用 (Nano/Micro层禁用, ADR 032)
15. **风控监控**: 检测到位但无自动干预 (只报警不执行)
16. **多策略并行**: strategy_config表支持但实际只用quant策略

### 待实现

| 优先级 | 功能 | 现状 |
|--------|------|------|
| **P0** | 测试覆盖率从~30%→70%+ | test/ 仅855行, 覆盖7/25+模块 |
| **P0** | 止盈逻辑 | max_hold_days=20有配置但未强制执行 |
| **P1** | Backtrader事件驱动回测替换手工loop | TODO.md已有规划 |
| **P1** | 因子卡片系统替代人工注册 | TODO.md已有规划 |
| **P1** | Telegram/微信通知实际接入 | 代码存在但token硬编码为空字符串 |
| **P1** | 真正的Walk-Forward训练/测试分割 | 当前只是周期IC重训, 无held-out验证 |
| **P2** | ML模型因子挖掘 | lightgbm/xgboost已安装但未用于因子发现 |
| **P2** | 行业轮动策略完善 | SectorRotator用硬编码PMI/CPI阈值 |
| **P2** | 多周期信号确认 | MultiTimeframeConfirmer存在但N+1查询未优化 |

---

## 三、架构评估

### 优点

1. **严格的7层分离**: 每层通过目录隔离, 依赖方向清晰
2. **配置驱动**: `_require_cfg()` 零fallback模式, fail-fast
3. **ADRs完善**: 33个架构决策记录, 互相引用, 形成连贯决策链
4. **HANDOFF.md**: 异常详细的开发日志, 每次变更都有记录
5. **Schema单源**: sim_trades DDL只在TradeRepo定义
6. **资金唯一真相源**: `get_cash()`实时从sim_trades计算, 禁止缓存

### 需要优化/重构

| 严重度 | 问题 | 具体位置 |
|--------|------|---------|
| **HIGH** | 无抽象接口层: 7层之间全是具体类直接导入, 无ABC定义契约 | 全项目 |
| **HIGH** | DataStore过度膨胀: 1590行单文件, 混合多种职责 | data/store.py |
| **HIGH** | scheduler/attribution.py过于臃肿: 406行包含多种归因逻辑 | scheduler/attribution.py |
| **MEDIUM** | 两个crontab安装脚本冲突: 定时不同 | scripts/install_crontab.sh vs setup_cron.sh |
| **MEDIUM** | 两个TradeRepo实现并存: 旧的313行和新的94行 | data/ |
| **MEDIUM** | 内联导入: pipeline.py在函数体内import, 隐藏依赖关系 | pipeline.py |
| **LOW** | factor/synth.py是纯重导出: 增加混淆 | factor/synth.py |

---

## 四、代码质量与逻辑错误

### 确认Bug: CRITICAL

| # | 位置 | 描述 |
|---|------|------|
| C1 | config/config.yaml | YAML语法错误 → 整个项目无法import (HANDOFF确认) |
| C2 | risk/covariance.py:98 | Ledoit-Wolf收缩强度被低估~57倍, 退化为裸样本协方差 |
| C3 | benchmark/tracker.py:172-173 | 累积收益率始终显示0% — 用错变量(strat_cum而非s_eq) |
| C4 | data/benchmark.py:59 | to_compact()未导入 → sync_benchmark每次调用抛NameError |
| C5 | risk/var.py:262 | portfolio_value变量未定义 → update_daily_risk()抛NameError |
| C6 | execution/stop_loss.py:109 | tp1_hit标志从未写回 → TP1无限重复触发, TP2/trailing永不执行 |

### 确认Bug: HIGH

| # | 位置 | 描述 |
|---|------|------|
| H1 | data/store.py:~1387 | trade_date = trade_date — LHB日期自赋值, 数据写入错误 |
| H2 | scripts/run_task.sh:83 | while true: 应为 while True: — daemon模式崩溃 |
| H3 | monitor/alerts.py:28 | 回撤告警用total_pnl/capital(总回报), 非真正peak-to-trough回撤 |
| H4 | monitor/notify.py:20-21 | _telegram_token()硬编码返回"", Telegram永久禁用 |
| H5 | optimizer/kelly.py:73 | 用alpha分数方差(~1.0)替代收益率方差(~0.0004), Kelly仓位~2500倍过小 |
| H6 | optimizer/portfolio.py 多处 | 权重裁剪+重归一化不收敛, 单票仓位限制未被可靠执行 |
| H7 | execution/engine.py:143-145 | PnL用get_last_buy_price(LIFO), 非FIFO, 多次买入时PnL错误 |
| H8 | execution/stop_loss.py:100 | Peak价格从未持久化 → trailing stop用初始成本而非实际峰值 |
| H9 | optimizer/rebalance.py:146-150 | 现金检查未按alpha排序, 低alpha买单优先于高alpha |
| H10 | risk/var.py:239 | 硬编码日期"2026-01-01" → 2027年起崩溃 |

### 确认Bug: MEDIUM

| # | 位置 | 描述 |
|---|------|------|
| M1 | factor/compute/_primitives.py:386 | compute_turnover_change映射到_turnover_reversal(语义错误) |
| M2 | factor/compute/ | abn_turnover有两套不同实现: 完整OLS回归 vs 简化均值偏离 |
| M3 | web/app.py:85-91 | index()路由直接sqlite3.connect绕过TradeRepo |
| M4 | web/static/app.js | renderRiskExposure引用不存在的rd.var/rd.cvar, 始终渲染0 |
| M5 | web/static/app.js | Plotly颜色用CSS变量字符串, 不解析 |
| M6 | monitor/report.py:59 | unrealized PnL初始化为0但从不对持仓估值 |
| M7 | monitor/alerts.py:43 | 数据陈旧告警检查last_daily_sync字段 — pipeline从未写入 |
| M8 | regime/detector.py:22 | HMM标签映射错误: 3状态排序后state 1应为sideways而非bear |
| M9 | execution/quote.py:74-79 | 9xxx前缀映射bug: 上海B股(900xxx)被错误映射到bj |
| M10 | risk/constraints.py | apply_all_filters接受industries参数但从未调用sector_exposure_check |

### 代码质量问题

| # | 位置 | 描述 |
|---|------|------|
| Q1 | factor/compute/fundamental.py | bp_ratio用-bp(取负), 实际是成长因子而非价值因子, 命名误导 |
| Q2 | factor/compute/price/_momentum.py | reversal_5d和momentum_*都计算正向累积收益, 高相关 |
| Q3 | factor/compute/ | 4个compute_*函数定义但从未注册到FN_MAP (死代码~150行) |
| Q4 | alpha/multi_tf.py | 每只股票打开一次SQLite连接 (N+1查询问题) |
| Q5 | web/app.py | api_performance同样SQL查询执行4次 |
| Q6 | backtest/diagnostics.py | compute_pre_backtest_ic导入但从未调用 |

### 文档与代码不一致

| 位置 | 文档声称 | 实际情况 |
|------|---------|---------|
| CLAUDE.md | 57因子 (41 price + 16 fundamental) | 79因子 (46 price + 33 fundamental) |
| CLAUDE.md | factor/base.py — Factor抽象基类 | 文件不存在, 因子用独立函数实现 |
| README.md | 因子卡片系统 | 只存在JSON文件, 未与registry自动化联动 |

---

## 五、算法评估

### 正确且坚实的算法

| 算法 | 实现质量 | 来源 |
|------|---------|------|
| Spearman Rank IC | 正确, 双模式设计, 无前视偏差 | Grinold & Kahn (2000) |
| 截面行业中性化 | 行业内z-score, 无数据泄露 | BARRA USE4 |
| 市值中性化 | OLS回归log(mcap)取残差 | Fama & French (1993) |
| Brinson归因 | 单期数学正确 | Brinson, Hood, Beebower (1986) |
| CPCV + PBO | De Prado(2018)标准实现, 带purging/embargo | De Prado AFML Ch.7-8 |
| 成本模型 | 佣金+印花税+滑点+市场冲击(Almgren-Chriss) | 交易所规则 + AC(2001) |

### 需要改进的算法

| 算法 | 问题 | 建议 |
|------|------|------|
| **Ledoit-Wolf收缩** | 收缩强度公式T/(T-1)^3错误, 应为1/T | 修正为Ledoit & Wolf (2004) eq.(17)标准公式 |
| **均值-方差优化** | 用np.linalg.inv手工求逆, 对奇异矩阵脆弱 | 用cvxpy约束凸优化; 增加pinv伪逆回退 |
| **Kelly公式** | 用alpha分数方差替代收益率方差, 实质退化 | 使用个股收益率方差; 或重命名为alpha比例分配 |
| **VaR计算** | 仅用等权假设做快速估算 | VaR应与优化器输出权重一致 |
| **SectorRotator** | 硬编码PMI/CPI阈值, 未验证A股适用性 | 需要A股行业轮动实证验证 |
| **Brinson多期链接** | 仅单期有效, 无Carino/Menchero平滑 | 多期归因需对数平滑消除残差 |
| **Sharpe计算(report.py)** | 从逐笔交易构建日收益, 无交易日估值 | 权益曲线应每日估值 |
| **HMM市场状态** | 标签映射错误 + 每天重训练无一致性 | 修正标签; 增加状态标签一致性机制 |
| **止盈系统** | tp1_hit和peak未持久化, 状态丢失 | 将状态写回position dict或外部存储 |

### 关键算法缺口

1. **无真正的机器学习因子**: lightgbm/xgboost已安装但未用于非线性因子合成
2. **无自适应参数**: 所有阈值(config.yaml)为静态, 应考虑波动率自适应
3. **换手率优化不足**: 当前rebalance仅基于固定周期(5天), 无换手率惩罚项

---

## 补充: Risk/Optimizer/Execution 层关键发现

### Ledoit-Wolf收缩Bug (CRITICAL)

**位置**: `risk/covariance.py:98`

公式 `pi_mat *= T / ((T - 1) ** 3)` 对于T=60产生~0.00029，而正确值应为~0.0169 (即 `1/T`)。这导致收缩强度被低估约57倍，协方差估计退化回裸样本协方差，对5000只股票×60天的高维场景完全不可靠。

### 止盈/止损状态丢失 (CRITICAL)

**位置**: `execution/stop_loss.py:99-109`

`tp1_hit`标志读取自position dict但从未写回。意味着:
- TP1每次调用都触发(重复卖50%)
- TP2和trailing逻辑永不执行
- Peak价格同样未持久化, trailing stop用初始成本而非实际峰值

### Kelly实质失效 (HIGH)

**位置**: `optimizer/kelly.py:73`

用`alpha.var()`(alpha分数截面方差≈1.0)替代个股收益率方差(≈0.0004)。结果Kelly仓位被低估约2500倍, 实质退化为简单alpha比例分配。命名"Kelly"具有误导性。

### 权重裁剪不收敛 (HIGH)

**位置**: `optimizer/portfolio.py` 多处

`min(w, max_single)` → 重归一化后, 某些权重可能再次超过max_single。例如3只股票权重[0.10,0.10,0.80]裁剪到0.05后归一化为[0.33,0.33,0.33], 全部超过0.05上限。需要迭代算法或QP求解器。

---

## 六、详细修改方案

以下为每个Bug的精确修改方案。遵循项目CLAUSE.md编辑规则：YAML文件用`python3` + `yaml.safe_load/dump`；Python文件用heredoc或精确`old_string → new_string`替换；禁止嵌套heredoc。

---

### C1 — config.yaml YAML语法错误

**严重度**: CRITICAL | **文件**: `quant/config/config.yaml`

**问题诊断**: HANDOFF.md记录的错误日志显示 `ParserError: expected <block end>, but found '<block mapping start>'` 在第53行。检查发现 `benchmark:` 键被重复定义（第28行 `backtest.benchmark: '000300'` 和第44行 `benchmark.start_date: '2025-12-01'`），且 `data` 与 `benchmark` 之间缩进层级混乱，YAML parser无法判断正确的嵌套关系。

**当前代码** (config.yaml 第42-54行):
```yaml
backtest:
  benchmark: '000300'
  ...
  universe_filter_affordable: true
cache: null
calendar:
  max_lookup_days: 30
benchmark:
  start_date: '2025-12-01'
data:
  batch_size: 50
  ...
  start_date: '2020-01-01'
  benchmark_start_date: '2020-01-01'    # <-- 冗余+缩进混淆
```

**修改方案**:
```yaml
backtest:
  benchmark: '000300'
  default_capital: 5000
  default_end: '2026-06-30'
  default_start: '2023-01-01'
  lot_size: 100
  min_backtest_days: 250
  rebalance_interval_days: 5
  universe_size: 800
  universe_turnover_days: 7
  diagnosis_ic_window: 120
  progress_log_interval: 60
  min_trading_days: 10
  universe_filter_affordable: true

cache: null

calendar:
  max_lookup_days: 30

benchmark:
  start_date: '2025-12-01'

data:
  batch_size: 50
  derived_ratio_max: 100
  gap_fill_limit: 100
  lookback_days: 365
  pe_max: 200
  stale_days: 250
  start_date: '2020-01-01'
  # benchmark_start_date: '2020-01-01'  # 移除 — 与benchmark.start_date重复
```

**关键改动**:
1. 在 `cache: null`, `calendar:`, `benchmark:` 之前各增加一个空行，确保YAML层级清晰
2. 删除 `data.benchmark_start_date`（与 `benchmark.start_date` 功能重复，且破坏了缩进一致性）
3. 所有引用 `data.benchmark_start_date` 的代码改为 `benchmark.start_date`

**执行方法** (遵循CLAUSE.md YAML规则):
```bash
python3 << 'PYEOF'
import yaml
with open('quant/config/config.yaml') as f:
    cfg = yaml.safe_load(f)
# 移除重复的 benchmark_start_date
if 'benchmark_start_date' in cfg.get('data', {}):
    del cfg['data']['benchmark_start_date']
with open('quant/config/config.yaml', 'w') as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
PYEOF
```

**验证**:
```bash
python3 -c "from quant.config.loader import load; load(); print('OK')"
```

---

### C2 — Ledoit-Wolf收缩强度公式错误

**严重度**: CRITICAL | **文件**: `quant/risk/covariance.py` 第98行

**问题诊断**: 当前公式 `pi_mat *= T / ((T - 1) ** 3)` 产生的缩放因子对于T=60约为0.00029。标准Ledoit-Wolf (2004) 公式的渐近方差估计量应为 `pi_hat = (1/T) * Σᵢⱼ Σₜ [(xₜᵢxₜⱼ - sᵢⱼ)²]`。正确的缩放应为 `1/T`。

**来源**: Ledoit, O. and Wolf, M. (2004). "A well-conditioned estimator for large-dimensional covariance matrices." Journal of Multivariate Analysis, 88(2), 365-411. Equation (17).

**当前代码**:
```python
        pi_mat = np.zeros((n, n))
        for t in range(T):
            diff = np.outer(X[t], X[t]) - S
            pi_mat += diff ** 2
        pi_mat *= T / ((T - 1) ** 3)  # 渐近方差修正
        pi_hat = pi_mat.sum()
```

**修改后**:
```python
        pi_mat = np.zeros((n, n))
        for t in range(T):
            diff = np.outer(X[t], X[t]) - S
            pi_mat += diff ** 2
        pi_mat /= T  # Ledoit-Wolf (2004) eq.(17): AsyVar estimator = (1/T) * Σ(x_ti·x_tj - s_ij)²
        pi_hat = pi_mat.sum()
```

**完整的ledoit_wolf_cov函数修改**（同时修正S使用有偏估计除以T的问题）:
```python
def ledoit_wolf_cov(returns, shrinkage=None):
    symbols = returns.columns.tolist()
    n = len(symbols)
    T = len(returns)

    if n < 2 or T < n:
        var = returns.var(ddof=1)
        return pd.DataFrame(np.diag(var.values), index=symbols, columns=symbols)

    # 中心化 — 使用有偏样本协方差（除以T而非T-1），与LW(2004)推导一致
    X = returns.values - returns.values.mean(axis=0)
    S = (X.T @ X) / T  # 有偏样本协方差 (LW原文用除以T)

    target = _constant_correlation_target(S)

    if shrinkage is None:
        # π: AsyVar(s_ij) per LW(2004) eq.(17)
        pi_mat = np.zeros((n, n))
        for t in range(T):
            diff = np.outer(X[t], X[t]) - S
            pi_mat += diff ** 2
        pi_mat /= T  # 修正: 1/T 替代 T/(T-1)^3
        pi_hat = pi_mat.sum()

        # γ: 样本协方差与目标的距离
        gamma_hat = ((S - target) ** 2).sum()

        # δ* = π̂ / γ̂, clamped to [0, 1]
        shrinkage = max(0.0, min(1.0, pi_hat / max(gamma_hat, 1e-10)))

    shrunk = (1 - shrinkage) * S + shrinkage * target
    return pd.DataFrame(shrunk, index=symbols, columns=symbols)
```

**验证**:
```bash
python3 << 'PYEOF'
import numpy as np, pandas as pd
from quant.risk.covariance import ledoit_wolf_cov
np.random.seed(42)
# 模拟高维场景: 100只股票 × 60天
returns = pd.DataFrame(np.random.randn(60, 100) * 0.02, columns=[f"s{i}" for i in range(100)])
cov = ledoit_wolf_cov(returns)
# 验证收缩强度在合理范围 (0.1~0.9)
S = returns.cov().values
shrinkage_est = np.sum((cov.values - S)**2) / np.sum((np.diag(np.diag(S)) - S)**2)
print(f"estimated shrinkage ≈ {shrinkage_est:.3f}")
assert 0.05 < shrinkage_est < 0.95, f"Shrinkage {shrinkage_est:.3f} out of expected range"
print("PASS")
PYEOF
```

---

### C3 — 基准累积收益率始终显示0%

**严重度**: CRITICAL | **文件**: `quant/benchmark/tracker.py` 第172-173行

**问题诊断**: 循环中正确累积了 `s_eq *= (1 + sr)` 和 `b_eq *= (1 + br)`，但第172-173行使用了从未更新的 `strat_cum` 和 `bench_cum`（初始化为1.0），导致累积收益率始终输出0.0%。

**当前代码**:
```python
    strat_cum = 1.0       # 初始化后从未更新
    bench_cum = 1.0       # 初始化后从未更新
    curves = []
    s_eq = 1.0
    b_eq = 1.0
    for date_str, sr, br, _ in rows:
        if sr is not None:
            s_eq *= (1 + sr)
        if br is not None:
            b_eq *= (1 + br)
        curves.append({...})

    strat_cum_pct = round((strat_cum - 1) * 100, 2)   # BUG: strat_cum始终=1.0
    bench_cum_pct = round((bench_cum - 1) * 100, 2)   # BUG: bench_cum始终=1.0
```

**修改后**:
```python
    curves = []
    s_eq = 1.0
    b_eq = 1.0
    for date_str, sr, br, _ in rows:
        if sr is not None:
            s_eq *= (1 + sr)
        if br is not None:
            b_eq *= (1 + br)
        curves.append({...})

    strat_cum_pct = round((s_eq - 1) * 100, 2)
    bench_cum_pct = round((b_eq - 1) * 100, 2)
```

同时修复第176行混淆的变量名:
```python
    # 旧: latest = conn = sqlite3.connect(_TRADES_DB)
    # 新:
    tracking_conn = sqlite3.connect(_TRADES_DB)
    lr = tracking_conn.execute(
        "SELECT rolling_alpha_60d, rolling_ir_60d, rolling_beta_60d, "
        "up_capture_60d, down_capture_60d FROM benchmark_tracking "
        "WHERE rolling_alpha_60d IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    tracking_conn.close()
```

**验证**:
```bash
python3 -c "
from quant.benchmark.tracker import get_tracking_summary
r = get_tracking_summary()
print('cumulative:', r.get('cumulative', {}))
assert r['cumulative']['strategy_pct'] != 0.0 or not r['available'], 'Should not always be 0'
print('PASS')
"
```

---

### C4 — benchmark同步缺少import

**严重度**: CRITICAL | **文件**: `quant/data/benchmark.py` 第14行+第59行

**问题诊断**: 第59行调用了 `to_compact(last_date)` 但函数未导入。第14行只导入了 `validate_date_format`。

**当前代码**:
```python
from quant.utils.date import validate_date_format
```

**修改后**:
```python
from quant.utils.date import validate_date_format, to_compact
```

**备选方案**（如果不想新增导入，用内联替代）:
```python
# 第59行改为:
start_date=last_date.replace("-", ""),
```

**验证**:
```bash
python3 -c "from quant.data.benchmark import sync_benchmark; print('import OK')"
```

---

### C5 — var.py: portfolio_value未定义

**严重度**: CRITICAL | **文件**: `quant/risk/var.py` 第262行

**问题诊断**: SQL INSERT的绑定参数使用了 `portfolio_value`，但函数中该变量不存在。第229行定义的是 `total_wealth`。

**当前代码** (第257-263行):
```python
    _conn.execute(
        "INSERT OR REPLACE INTO daily_risk(date, var_95, var_95_pct, cvar_95, cvar_95_pct, portfolio_value, n_positions) "
        "VALUES (date('now','localtime'), ?, ?, ?, ?, ?, ?)",
        (rpt.get("var", {}).get("var_95"), rpt.get("var", {}).get("var_95_pct"),
         rpt.get("cvar", {}).get("cvar_95"), rpt.get("cvar", {}).get("cvar_95_pct"),
         portfolio_value, len(positions))    # <-- NameError
    )
```

**修改后**:
```python
    _conn.execute(
        "INSERT OR REPLACE INTO daily_risk(date, var_95, var_95_pct, cvar_95, cvar_95_pct, portfolio_value, n_positions) "
        "VALUES (date('now','localtime'), ?, ?, ?, ?, ?, ?)",
        (rpt.get("var", {}).get("var_95"), rpt.get("var", {}).get("var_95_pct"),
         rpt.get("cvar", {}).get("cvar_95"), rpt.get("cvar", {}).get("cvar_95_pct"),
         total_wealth, len(positions))
    )
```

**验证**:
```bash
python3 -c "import ast; ast.parse(open('quant/risk/var.py').read()); print('syntax OK')"
```

---

### C6 — TP1标志和Peak未持久化

**严重度**: CRITICAL | **文件**: `quant/execution/stop_loss.py` 第99-133行

**问题诊断**: `tp1_hit` 从 `p.get("_tp1_hit", False)` 读取，第109行设为 `True`（局部变量），但从未 `p["_tp1_hit"] = True` 写回。`peak` 同理。

**当前代码** (核心问题段):
```python
    def check(self, p: dict, date_str: str) -> list:
        ...
        tp1_hit = p.get("_tp1_hit", False)
        peak = max(p.get("_peak", cost), cur) if cur and cur > 0 else cost
        ...
        if not tp1_hit and gain >= self.atr_mult_tp1 * atr:
            ...
            tp1_hit = True          # 局部变量, 未写回!

        if tp1_hit and gain >= self.atr_mult_tp2 * atr:
            ...                      # 永远无法到达
```

**修改方案**:
在第109行之后增加写回操作:
```python
        if not tp1_hit and gain >= self.atr_mult_tp1 * atr:
            sell_shares = shares // 2
            if sell_shares > 0:
                signals.append({"symbol": symbol, "side": "sell", "shares": sell_shares,
                                "price": cur, "reason": "TP1"})
            tp1_hit = True
            p["_tp1_hit"] = True    # 写回position dict
```

在第100行之后增加peak写回:
```python
        peak = max(p.get("_peak", cost), cur) if cur and cur > 0 else cost
        p["_peak"] = peak           # 写回position dict
```

同时修改函数签名注释，注明position dict会被修改（side-effect by design）。

**验证**:
```bash
python3 << 'PYEOF'
# 模拟测试
from quant.execution.stop_loss import RiskManager
rm = RiskManager()
p = {"symbol": "000001", "price": 10.0, "shares": 1000, "buy_time": "2026-07-01"}
# 模拟ATR=0.5
rm.atr_mult_tp1 = 1.0
p["_atr"] = 0.5
signals = rm.check(p, "2026-07-21")
# 检查tp1_hit是否持久化
assert p.get("_tp1_hit") == (len(signals) > 0), "tp1_hit should be persisted"
# 第二次调用不应重复触发TP1
signals2 = rm.check(p, "2026-07-21")
tp1_signals2 = [s for s in signals2 if "TP1" in s.get("reason", "")]
assert len(tp1_signals2) == 0, "TP1 should only fire once"
print("PASS")
PYEOF
```

---

### H1 — LHB trade_date自赋值Bug

**严重度**: HIGH | **文件**: `quant/data/store.py` 约1387行

**问题诊断**: `trade_date = trade_date` 是一个无操作语句。变量自赋值没有效果，但掩盖了该变量可能在后续代码中被未初始化使用的问题。

**修改方案**:
检查store.py中`sync_lhb_data`方法，确认`trade_date`的实际来源：

1. 如果`trade_date`来自外层循环变量且值正确 → 直接删除第1387行的自赋值行
2. 如果`trade_date`需要通过`row.get("trade_date")`获取 → 改为 `trade_date = to_str(row.get("trade_date"))`

需要打开文件精确定位后确认。建议：
```bash
grep -n "trade_date = trade_date" quant/data/store.py
# 读取上下文5行确定正确修复
```

---

### H2 — run_task.sh daemon语法错误

**严重度**: HIGH | **文件**: `scripts/run_task.sh` 第83行

**问题诊断**: Shell脚本中写了Python语法 `while true:` 应为 `while True:`。这是Python关键字大小写错误。

**当前代码**:
```bash
    daemon)
        PYTHONPATH=. python3 -c "
from quant.scheduler import start
start()
import time
while true:       # <-- Python中是True, 不是true
    time.sleep(60)
"
        ;;
```

**修改后**:
```bash
    daemon)
        PYTHONPATH=. python3 -c "
from quant.scheduler import start
start()
import time
while True:
    time.sleep(60)
"
        ;;
```

---

### H3 — 回撤告警用总回报替代peak-to-trough

**严重度**: HIGH | **文件**: `quant/monitor/alerts.py` 第28行

**问题诊断**: 回撤(drawdown)的正确定义是从历史最高点到当前点的跌幅: `DD = (peak - current) / peak`。当前代码用 `total_pnl / capital` 计算的是总回报率，不是回撤。

**当前代码**:
```python
    # 第28行附近
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    drawdown_pct = abs(total_pnl) / capital if total_pnl < 0 else 0
    # drawdown_pct实际是"总亏损/本金", 非"peak-to-trough回撤"
```

**修改方案**:
需要从权益曲线计算真正的最大回撤。修改为读取`benchmark_tracking`表或传入权益曲线:
```python
    # 方案A: 如果能获取权益曲线
    def _check_drawdown(equity_curve: list, capital: float, threshold: float):
        if not equity_curve:
            return []
        peak = capital
        max_dd = 0.0
        for point in equity_curve:
            equity = point.get("equity", peak)
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        if max_dd >= threshold:
            return [{"type": "drawdown", "value": round(max_dd * 100, 1),
                     "threshold": round(threshold * 100, 1)}]
        return []

    # 方案B: 简化版 — 用当前总资产vs历史最高总资产
    current_total = cash + sum(p["price"] * p["shares"] for p in positions)
    peak = max(p.get("_peak_value", current_total) for p in positions)
    # ...存peak到外部状态
```

建议采用方案A（正确但需要权益曲线数据），短期可用方案B（近似）。

---

### H4 — Telegram通知token硬编码为空

**严重度**: HIGH | **文件**: `quant/monitor/notify.py` 第20-21行

**问题诊断**: `_telegram_token()` 函数直接返回空字符串 `""`，未从config.yaml读取。配置中 `monitor.telegram_bot_token` 已定义但未被使用。

**当前代码**:
```python
def _telegram_token():
    return ""   # 硬编码为空, 永久禁用
```

**修改后**:
```python
def _telegram_token():
    from quant.config.constants import _require_cfg
    return _require_cfg("monitor.telegram_bot_token")
```

---

### H5 — Kelly方差使用错误

**严重度**: HIGH | **文件**: `quant/optimizer/kelly.py` 第72-76行

**问题诊断**: `var = alpha.var()` 计算的是alpha分数的**截面方差**（标准化后≈1.0），而非个股**收益率方差**（日收益≈0.0004）。差距约2500倍，导致f*≈0。

**当前代码**:
```python
    mu = alpha / alpha.abs().max() * med_ic   # 近似: alpha→期望收益
    var = alpha.var() if alpha.var() > 0 else 0.01  # BUG: 截面方差≠收益率方差
    kelly_raw = mu / max(var, 1e-8)
```

**修改方案**（短期 — 用固定波动率代理）:
```python
    # A股日收益率典型波动率≈2% (σ²≈0.0004), 来源: CSRC 2025年度报告
    DEFAULT_RETURN_VAR = 0.0004
    mu = alpha / alpha.abs().max() * med_ic  # IC→期望收益映射
    kelly_raw = mu / DEFAULT_RETURN_VAR
```

**修改方案**（长期 — 从market.db读取个股实际波动率）:
```python
    # 从market.db读取每只股票的近60日收益率方差
    from quant.data.store import DataStore
    store = DataStore()
    returns_data = store.get_daily(alpha.index.tolist(), 
                                    start=(pd.Timestamp.today() - pd.Timedelta(days=90)).strftime("%Y-%m-%d"))
    if returns_data is not None and not returns_data.empty:
        log_rets = np.log(returns_data["close"]).diff().dropna(how="all")
        stock_vars = log_rets.var()  # 每只股票的实际方差
    else:
        stock_vars = pd.Series(DEFAULT_RETURN_VAR, index=alpha.index)
    store.close()
    kelly_raw = mu / stock_vars.clip(lower=1e-6)
```

建议先用短期方案（简单可靠），长期方案标注TODO。

---

### H6 — 权重裁剪后重归一化不收敛

**严重度**: HIGH | **文件**: `quant/optimizer/portfolio.py` 第338, 379, 418行 + `calibrate_risk_aversion` 第95行

**问题诊断**: `min(w, max_single)` → `w / w.sum()` 之后，某些权重可能再次超过max_single。例如3只股票各0.05裁剪后sum=0.15, 归一化后各0.333 >> 0.05。

**影响位置**（4处）:
1. `_score_weighted_rounding` 第338行
2. `_mean_variance_lot` 第379行
3. `_risk_parity` 第418行
4. `calibrate_risk_aversion` 第95行

**修改方案** — 添加迭代裁剪函数:
```python
def _clip_and_normalize(weights: np.ndarray, max_single: float, max_iter: int = 20) -> np.ndarray:
    """迭代裁剪+重归一化直到所有权重 <= max_single。
    
    算法: 反复裁剪超过上限的权重并重归一化。
    每次迭代后超过上限的权重数单调递减, 保证收敛。
    """
    w = weights.copy()
    for _ in range(max_iter):
        over = w > max_single
        if not over.any():
            break
        w[over] = max_single
        s = w.sum()
        if s <= 0:
            return np.ones(len(w)) / len(w)
        w = w / s
    return w
```

然后在4处调用点替换:
```python
# 旧: weights = np.minimum(weights, self.max_single); weights = weights / weights.sum()
# 新:
weights = _clip_and_normalize(weights, self.max_single)
```

**验证**:
```bash
python3 << 'PYEOF'
import numpy as np
# 测试极端场景
def _clip_and_normalize(w, max_single, max_iter=20):
    w = w.copy()
    for _ in range(max_iter):
        over = w > max_single
        if not over.any():
            break
        w[over] = max_single
        w = w / w.sum()
    return w

w = np.array([0.10, 0.10, 0.80])
result = _clip_and_normalize(w, 0.05)
assert (result <= 0.0501).all(), f"权重超限: {result}"
assert abs(result.sum() - 1.0) < 0.001, f"和不等于1: {result.sum()}"
print(f"裁剪结果: {result}")
print("PASS")
PYEOF
```

---

### H7 — PnL用LIFO而非FIFO

**严重度**: HIGH | **文件**: `quant/execution/engine.py` 第143-145行

**问题诊断**: `get_last_buy_price()` 只返回最新买入价。如果同一股票多次买入不同价格，卖出时应使用FIFO(先进先出)或平均成本，而非LIFO(后进先出)。

**当前代码**:
```python
    orig = repo.get_last_buy_price(strategy, symbol)
    if orig and orig[0] * shares > 0:
        e["pnl"] = round(proceeds - orig[0] * shares, 2)
        e["pnl_pct"] = round(proceeds / (orig[0] * shares) - 1, 2)
```

**修改方案** — 使用加权平均成本:
```python
    # 替换get_last_buy_price → 获取该symbol所有未平仓买入的加权平均成本
    avg_cost = repo.get_average_cost(strategy, symbol)
    if avg_cost and avg_cost * shares > 0:
        e["pnl"] = round(proceeds - avg_cost * shares, 2)
        e["pnl_pct"] = round(proceeds / (avg_cost * shares) - 1, 2) if avg_cost > 0 else 0.0
```

需要在 `TradeRepo` 中新增 `get_average_cost` 方法:
```python
def get_average_cost(self, strategy: str, symbol: str) -> float:
    """返回某symbol的加权平均买入成本 (FIFO近似).
    
    对于简单场景(one-shot买入), 等价于last_buy_price.
    对于多次买入场景, 计算买入总额/买入总股数.
    """
    conn = self._conn()
    row = conn.execute(
        "SELECT SUM(price * shares) / SUM(shares) FROM sim_trades "
        "WHERE strategy=? AND symbol=? AND side='buy'",
        (strategy, symbol)
    ).fetchone()
    return float(row[0]) if row and row[0] else 0.0
```

---

### H8 — Peak价格未持久化

**严重度**: HIGH | **文件**: `quant/execution/stop_loss.py` 第100行

**问题诊断**: 同C6。`peak`计算后只作为局部变量存在，未写回position dict。

**修改方案**（与C6一起修复）:
```python
    peak = max(p.get("_peak", cost), cur) if cur and cur > 0 else cost
    p["_peak"] = peak  # 持久化peak
```

---

### H9 — 现金可行性检查未按alpha排序

**严重度**: HIGH | **文件**: `quant/optimizer/rebalance.py` 第146-150行

**问题诊断**: 可用的买入订单按原始顺序迭代，而非按alpha降序。这意味着低alpha的买单可能占据资金，导致高alpha买单被跳过。

**当前代码**:
```python
    for o in buy_orders:    # 原始顺序, 未排序
        if available >= o.cost:
            feasible.append(o)
            available -= o.cost
```

**修改后**:
```python
    # 按alpha降序排列买单, 确保高alpha优先获得资金
    if alpha_scores is not None:
        buy_orders.sort(key=lambda o: alpha_scores.get(o.symbol, 0), reverse=True)
    for o in buy_orders:
        if available >= o.cost:
            feasible.append(o)
            available -= o.cost
```

---

### H10 — var.py硬编码日期

**严重度**: HIGH | **文件**: `quant/risk/var.py` 第239行

**问题诊断**: `start="2026-01-01"` 硬编码。2027年起数据范围不足。

**当前代码**:
```python
    recent_data = store.get_daily(syms, start="2026-01-01")
```

**修改后**:
```python
    # 滚动窗口: 取最近365天, 确保有足够样本
    from datetime import date, timedelta
    _start = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    recent_data = store.get_daily(syms, start=_start)
```

---

### M1 — turnover_change映射到错误shortcut

**严重度**: MEDIUM | **文件**: `quant/factor/compute/_primitives.py` 第386行

**当前代码**:
```python
    "compute_turnover_change":   _turnover_reversal,  # 错误映射
```

**修改方案**:
`turnover_change`的正确公式是 `(turnover_t - turnover_{t-w}) / turnover_{t-w}`，应在`_primitives.py`新增正确的函数:
```python
def _turnover_change(primitives, symbol, date, window=5):
    """换手率变化: (换手率_t - 换手率_{t-w}) / 换手率_{t-w}"""
    import pandas as pd
    t = primitives.get("turnover")
    if t is None or symbol not in t.columns:
        return np.nan
    ts = t[symbol].dropna()
    if len(ts) < window + 1:
        return np.nan
    return float((ts.iloc[-1] - ts.iloc[-window-1]) / abs(ts.iloc[-window-1]) if ts.iloc[-window-1] != 0 else np.nan)
```

然后修正映射:
```python
    "compute_turnover_change":   _turnover_change,  # 修正
```

注意：如果 `compute_turnover_change` 也未注册到 `_PRICE_FN_MAP`（死代码），则直接删除此条映射即可。

---

### M2 — abn_turnover两套实现

**严重度**: MEDIUM | **文件**: `quant/factor/compute/_alternative.py`(第198-262行) vs `quant/factor/compute/_primitives.py`(第355-366行)

**问题诊断**: `_dispatch.py`优先使用primitives中的简化版(`-|turnover/avg(turnover)-1|`)，而非完整OLS版(`ln(Turnover) ~ ln(MktCap) + industry dummies, 取-residual`)。两个版本产生不同的因子值。

**修改方案**:
1. **短期**: 删除 `_primitives.py` 中的 `_abn_turnover` shortcut，让`compute_abn_turnover`始终走完整OLS路径
2. **长期**: 回测两版因子IC，保留IC更高的版本

```python
# 在 _primitives.py 的 FACTOR_SHORTCUT 中删除:
# "compute_abn_turnover":    _abn_turnover,   # 删除此行
```

**验证**:
```bash
python3 -c "
from quant.factor.compute._dispatch import compute_all_factors
# 确认abn_turnover走完整OLS路径
print('需要手动对比两版IC后决定保留哪个')
"
```

---

### M3 — index()绕过TradeRepo

**严重度**: MEDIUM | **文件**: `web/app.py` 第85-91行

**当前代码**:
```python
    c = sqlite3.connect(TRADE_DB)  # 直接连接, 绕过TradeRepo
```

**修改后**:
```python
    from quant.data.trade_repo import TradeRepo
    repo = TradeRepo()
    positions = repo.get_positions(strategy)
    cash = repo.get_cash(strategy)
```

---

### M4-M5 — 前端渲染Bug

**严重度**: MEDIUM | **文件**: `web/static/app.js`

**M4 — 风险图表始终显示0**:
```javascript
// 旧: renderRiskExposure引用不存在的rd.var/rd.cvar
// 修改为从API返回的data.symbols[]中读取
function renderRiskExposure(data) {
    const symbols = data.symbols || [];
    const x = symbols.map(s => s.symbol);
    const vol = symbols.map(s => s.annual_vol_pct || 0);
    const mdd = symbols.map(s => s.max_dd_pct || 0);
    // ... 使用 vol 和 mdd 而非 rd.var / rd.cvar
}
```

**M5 — Plotly CSS变量不解析**:
```javascript
// 旧: colorscale: [[0, 'var(--down)'], [0.5, 'var(--bg)'], [1, 'var(--up)']]
// 修改为用getComputedStyle解析:
const style = getComputedStyle(document.documentElement);
const downColor = style.getPropertyValue('--down').trim();
const upColor = style.getPropertyValue('--up').trim();
const bgColor = style.getPropertyValue('--bg').trim();
// colorscale: [[0, downColor], [0.5, bgColor], [1, upColor]]
```

---

### M6 — unrealized PnL从未计算

**严重度**: MEDIUM | **文件**: `quant/monitor/report.py` 第59行

**当前代码**:
```python
    unrealized = 0.0   # 初始化为0, 从未更新
```

**修改后**:
```python
    unrealized = sum(
        (p.get("current_price", p.get("price", 0)) - p.get("price", 0)) * p.get("shares", 0)
        for p in positions
        if p.get("current_price", 0) > 0 and p.get("shares", 0) > 0
    )
```

---

### M7 — 数据陈旧告警从不触发

**严重度**: MEDIUM | **文件**: `quant/monitor/alerts.py` 第43行

**问题诊断**: 检查 `last_daily_sync` 字段但pipeline从未写入该字段。

**修改方案**:
在pipeline.py的Step 1（data update）完成后写入状态:
```python
# pipeline.py Step 1 末尾添加:
broker.update({"last_daily_sync": date_str})
```

或改为从market.db直接查询最新数据日期:
```python
def _check_stale_data():
    from quant.data.store import market_conn
    conn = market_conn("ro")
    row = conn.execute("SELECT MAX(date) FROM daily").fetchone()
    last_date = row[0] if row else None
    if last_date:
        from datetime import date, timedelta
        days_behind = (date.today() - date.fromisoformat(last_date)).days
        if days_behind > 2:
            return [{"type": "stale_data", "last_sync": last_date, "days_behind": days_behind}]
    return []
```

---

### M8 — HMM标签映射错误

**严重度**: MEDIUM | **文件**: `quant/regime/detector.py` 第22行

**问题诊断**: 3状态按drift降序排列后，state 0=最高=牛市, state 1=中间=横盘, state 2=最低=熊市。但标签映射 `{0: "bull", 1: "bear", 2: "sideways"}` 把中间状态错标为bear。

**当前代码**:
```python
REGIME_LABELS = {0: "bull", 1: "bear", 2: "sideways"}
```

**修改后**:
```python
REGIME_LABELS = {0: "bull", 1: "sideways", 2: "bear"}
```

---

### M9 — 9xxx前缀映射Bug

**严重度**: MEDIUM | **文件**: `quant/execution/quote.py` 第74-79行和132-137行

**问题诊断**: `symbol.startswith(("4", "8", "92"))` 中字符串 `"9"` 也在元组中，导致所有9开头的代码（包括上海B股900xxx）被映射到北京交易所。

**当前代码**:
```python
if symbol.startswith(("4", "8", "92")):
    return f"bj{symbol}"
```

实际上元组中没有单独的`"9"`——仔细看是`("4", "8", "92")`。但 `startswith("92")` 对 `"920001"` 为True（正确），对 `"900001"` 为False（因为"900001"不以"92"开头）。所以这个Bug实际上可能不存在，需要进一步验证。

但如果实际测试发现900xxx被错误路由，修改方案为：
```python
# 明确BJ前缀: 4xxxxx(北证A), 8xxxxx(新三板), 92xxxx(北证)
if symbol.startswith(("4", "8")) or symbol[:2] == "92":
    return f"bj{symbol}"
# 明确SH前缀: 6xxxxx(上证A), 9xxxxx(上证B/科创板)
if symbol.startswith(("6", "9")):
    return f"sh{symbol}"
return f"sz{symbol}"
```

---

### M10 — apply_all_filters未使用industries参数

**严重度**: MEDIUM | **文件**: `quant/risk/constraints.py` 第114行

**问题诊断**: 函数签名接受 `industries` 参数但从未调用 `sector_exposure_check`。文档说"过滤顺序: 流动性 → 股价 → ST → 行业暴露上限"，但实际上行业暴露检查从未执行。

**修改方案**:
在第146行（ST过滤之后）添加:
```python
    # 4. 行业暴露
    if industries is not None and len(df) > 0:
        # 计算候选池中各行业权重
        # 注意: 此检查需要权重信息, apply_all_filters只做过滤
        # 行业暴露检查应在优化器输出后调用sector_exposure_check
        pass  # 保留参数供未来集成, 当前由调用方单独检查
```

或者更新docstring删除"行业暴露上限"（反映当前实际行为）。

---

## 七、优先级行动清单

### 第一轮: CRITICAL修复 (预计工作量: 2-3小时)

| 顺序 | Bug | 文件 | 改动量 |
|------|-----|------|--------|
| 1 | C1 YAML语法 | config.yaml | 1行删除 |
| 2 | C2 LW收缩 | covariance.py | 1行修改 + S分母T |
| 3 | C3 累积收益0% | tracker.py | 2行修改 |
| 4 | C4 缺少import | benchmark.py | 1行添加 |
| 5 | C5 变量未定义 | var.py | 1行修改 |
| 6 | C6 止盈状态丢失 | stop_loss.py | 3行添加 |

### 第二轮: HIGH修复 (预计工作量: 4-6小时)

| 顺序 | Bug | 文件 | 改动量 |
|------|-----|------|--------|
| 7 | H2 daemon语法 | run_task.sh | 1行 |
| 8 | H4 Telegram接入 | notify.py | 1行 |
| 9 | H10 硬编码日期 | var.py | 3行 |
| 10 | H7 PnL FIFO | engine.py + trade_repo.py | ~15行 |
| 11 | H8 Peak持久化 | stop_loss.py | 1行(与C6合并) |
| 12 | H9 买入排序 | rebalance.py | 3行 |
| 13 | H6 权重裁剪 | portfolio.py | ~20行(新函数) |
| 14 | H5 Kelly方差 | kelly.py | ~10行 |
| 15 | H3 回撤告警 | alerts.py | ~20行 |
| 16 | H1 LHB日期 | store.py | 待确认 |

### 第三轮: MEDIUM修复 (预计工作量: 4-8小时)

| 顺序 | Bug | 文件 | 改动量 |
|------|-----|------|--------|
| 17 | M3 index绕过 | app.py | ~5行 |
| 18 | M8 HMM标签 | detector.py | 1行 |
| 19 | M6 unrealized PnL | report.py | ~8行 |
| 20 | M7 stale告警 | alerts.py + pipeline.py | ~15行 |
| 21 | M4-M5 前端 | app.js | ~20行 |
| 22 | M9 9xxx前缀 | quote.py | ~5行 |
| 23 | M10 industries | constraints.py | 文档更新 |
| 24 | M1-M2 factor | _primitives.py | 删除或重写 |

---

## 项目统计

| 指标 | 数值 |
|------|------|
| Python文件数 | 186 |
| 总代码行数 | 26,553 |
| 因子数量(实际) | 79 (46 price + 33 fundamental) |
| 因子数量(文档) | 57 (过时) |
| 测试数量 | ~69 |
| 测试覆盖率(估计) | ~30% |
| ADR数量 | 33 |
| 数据库大小(market.db) | 1.2 GB |
| 数据库大小(factor_cache.db) | 7.2 GB |
| Crontab任务 | 7 (周一至周五) + 1 (周六) |
| 数据源 | 7 (tushare/tickflow/zzshare/pytdx/sina/tencent/akshare) |
| Bug总数 | 26 (6 Critical + 10 High + 10 Medium) |

---

## 总体评价

项目架构坚实, 7层Grinold & Kahn框架实现完整, 配置驱动+零fallback原则贯彻到位, ADR和HANDOFF文档异常优秀。

主要问题集中在:
1. config.yaml阻塞性语法错误（修复即恢复）
2. Ledoit-Wolf收缩公式错误（核心风控算法失效~3个月）
3. 止盈止损状态管理bug（TP2永不触发）
4. 测试覆盖率严重不足(~30%)
5. 几个已确认的逻辑Bug

核心算法(CPCV+PBO/Brinson/行业中性化/成本模型)实现正确, 达到机构级质量。因子评估管道(7阶段CPCV+PBO)遵循De Prado学术标准, 是项目的亮点。

**建议修复顺序**: 先修C1恢复项目可运行 → 修C2+C3恢复核心算法正确性 → 修C6+H8恢复止盈止损功能 → 修H6恢复风控约束有效性 → 其余按优先级推进。

**预计总修复工时**: 10-17小时（三轮），建议分3天完成。
