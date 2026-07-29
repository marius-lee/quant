# 系统功能缺口分析 — 对标业界标准

> 2026-07-29 | test-v250

## 一、已有能力速览

| 功能域 | 状态 | 覆盖 |
|--------|------|------|
| 7 层架构 (Grinold & Kahn) | ✅ | 数据→因子→Alpha→风控→优化→执行→监控 |
| 65+ 因子 (价格+基本面) | ✅ | 动量/反转/波动率/量价/事件/情绪/估值/质量 |
| 因子物化缓存 (gzip CSV) | ✅ | 1590 天 ~ 1.5GB |
| Walk-forward 回测 | ✅ | Purged CPCV + 5-fold |
| 资本自适应三层组合 | ✅ | Nano(<3万)/Micro(3-10万)/Small(>10万) |
| Ledoit-Wolf 协方差 | ✅ | 收缩估计 |
| HMM 市场体制 | ✅ | 3 状态 (牛/熊/震荡) |
| Almgren-Chriss 冲击 | ✅ | sqrt(Q/V) 基础公式 |
| LightGBM 非线性 alpha | ✅ | 模型框架已完成 |
| DSR 多重检验 | ✅ | Bailey & De Prado 2014 |
| Web 仪表盘 | ✅ | 实时信号/持仓/绩效 |
| 8 阶段因子评估 | ✅ | CPCV + Walk-forward + PBO |

---

## 二、对标业界标准 — 缺失功能

### 2.1 因子研究层 (WorldQuant / AQR 标准)

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **因子半衰期分析** | 部分 (decay_horizons 配置存在，未接入评估流程) | WorldQuant: 每个因子必测 IC decay 曲线，half-life < 20 天标记为快衰减 | **P1** |
| **因子相关性矩阵 / 聚类** | 仅有 4 处引用 (ADR-038 冗余同向检测) | Barra: GICS 行业×风格因子协方差分解; DolphinDB: 因子聚类去冗余 | **P1** |
| **因子拥挤度** | 模块存在 (crowdedness.py) 但 `except: return None` 吞错 | AQR: 每个因子跟踪持仓拥挤度 (HHI/相关系数/多空对冲成本) | **P1** |
| **因子分位数回测** (quantile analysis) | **0** | Alphalens: 按因子值分 5/10 组，看各组累积收益单调性 | **P2** |
| **因子 IC 衰减曲线** | IC + ICIR 有，衰减曲线无 | Qlib: IC decay-by-lag 序列 → half-life 估计 | **P2** |
| **另类数据因子** | 初步 (sentiment/news 仅占位) | WorldQuant: 卫星图像/供应链/社交媒体/信用卡消费数据 | **P4** |
| **因子日历效应** | 6 处引用，未系统化 | AQR: 月末/季末/财报季前后的因子收益季节性检验 | **P3** |

### 2.2 Alpha 模型层 (Qlib / Two Sigma 标准)

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **模型可解释性 (SHAP)** | 0 | Qlib: SHAP values 分解每个因子对 alpha 的边际贡献 | **P2** |
| **多模型集成** (ensemble) | 仅 LGB | Qlib: DoubleEnsemble (LGB+TabNet); 多模型投票/平均 | **P3** |
| **在线学习 / 增量训练** | 1 处引用 (warm_start) | Two Sigma: 每日/每周增量更新模型权重的在线算法 | **P3** |
| **特征工程管线** | 0 (因子即特征, 无变换) | Qlib: 标准化/中性化/交互特征/时序差分特征 自动化 | **P2** |
| **滚动训练 + 时间序列交叉验证** | Walk-forward 有，但非模型训练用 | Qlib: RollingTrainer + PurgedGroupTimeSeriesSplit | **P2** |

### 2.3 组合优化层 (Barra / MSCI 标准)

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **组合约束** (max/min weight, cardinality) | **0** (仅 max_single_position) | Barra: 权重上下限/行业偏离/换手率上限/成分股数量约束 | **P1** |
| **风险预算** (risk budgeting) | HRP 有层次分配 | Barra: 按因子暴露分配风险预算 (Risk Parity → Risk Budgeting) | **P2** |
| **换手率约束** (turnover constraint) | 0 | Grinold & Kahn: 交易成本带 (§8.3 `tc_lambda`) 有拦截，但无硬约束 | **P2** |
| **协方差矩阵调整** (Newey-West, eigenfactor) | 仅 Ledoit-Wolf | Barra: Eigenfactor Risk Adjustment; MSCI: Newey-West 自相关修正 | **P3** |

### 2.4 执行层 (Kissell / ITG 标准)

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **交易成本分析 (TCA)** | **0** | ITG: 执行后滑点 vs 到达价/区间均价/VWAP 多维度 TCA | **P2** |
| **执行算法** (VWAP/TWAP/Implementation Shortfall) | 0 (仅 MARKET 市价) | Kissell: VWAP/TWAP/IS/POV 算法选择 + 参数优化 | **P3** |
| **I-Star 冲击模型** | 仅 Almgren-Chriss | Kissell: I-Star 模型 (含临时/永久冲击分离) 更精确 | **P4** |

### 2.5 监控与报告层 (Alphalens / Zipline 标准)

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **回测报告/Tear Sheet** | 17 处引用, 仅基础指标 | Alphalens: 完整的 HTML/PDF 报告 — IC 图/因子收益图/换手率/行业暴露 | **P2** |
| **实盘 vs 回测漂移检测** | Phase 8 模块存在但未接入执行链 | Quantopian: 每日对比 live 信号 vs backtest 预测的偏差 | **P2** |
| **绩效归因** (Brinson) | ✅ Brinson 行业归因已有 | OK | — |
| **因子归因** (factor PnL) | ✅ factor_pnl_attribution 已有 | OK | — |
| **风险归因** (risk decomposition) | 0 | Barra: 系统风险 vs 特质风险分解 + factor exposure 分解 | **P3** |

### 2.6 数据与基础设施

| 功能 | 现状 | 业界要求 | 优先级 |
|------|------|---------|--------|
| **多频数据** (分钟/小时) | 83 处引用 (tickflow/akshare) 但因子层未使用 | Qlib: 日频 + 分钟频 (alpha158/alpha360 双数据集) | **P3** |
| **实时行情推送** | quote.py 轮询 | 券商: WebSocket 推送 + Level-2 盘口 | **P4** |
| **数据版本管理** | 0 | DolphinDB: 数据快照 + 版本回溯; Quantopian: 数据时点锁定 | **P4** |
| **自动化超参搜索 + 结果持久化** | Optuna hyperopt 可用但 persist 未集成 | Qlib: Optuna + MLflow 实验追踪; 参数→metrics 版本化 | **P3** |

---

## 三、优先级建议

| 优先级 | 功能 | 理由 |
|--------|------|------|
| **P1** | 因子半衰期 + 相关性去冗余 | 直接影响因子池质量 — IC 高但互相关 0.9 的因子是假多样性 |
| **P1** | 因子拥挤度修复 + 接入评估链 | 已有代码, 吞错 bug 修掉即可 |
| **P1** | 组合约束 (max/min weight, cardinality) | 当前优化器产出理论上最优但无法执行的持仓 (如 0.1% 仓位无意义) |
| **P2** | 因子 IC 衰减曲线 (decay-by-lag) | WorldQuant 因子准入标准, G1 阶段应包含 |
| **P2** | 因子分位数回测 (quantile returns) | Alphalens 基础诊断, 验证因子方向性 + 单调性 |
| **P2** | 模型 SHAP 可解释性 | 金融机构合规要求; LGB 黑盒不可接受 |
| **P2** | 回测 Tear Sheet 报告 | 每次回测后自动生成, 无需手动 python -c |
| **P2** | TCA 交易成本分析 | 评估 CostModel 精度, 反馈给滑点参数 |
| **P2** | 滚动训练 + 时间序列 CV | 防止模型过拟合; 当前仅 walk-forward 回测, 训练无 CV |
| **P3** | 多模型集成 (DoubleEnsemble) | LGB 已就绪, 加 TabNet/简单 NN 提升稳定性 |
| **P3** | 多频数据因子 | 分钟频数据已有, 因子层扩展 |
| **P4** | 另类数据 / Level-2 / 实时推送 | 需要外部数据源 / 资金投入 |

---

## 四、当前最大风险

1. **因子集合可能严重冗余** — 65 因子无相关性矩阵, 可能 50+ 个高度相关。Alpha 合成的 sleeve/composite 模式在高度相关输入下退化。
2. **模型无 CV** — LGB 训练无时间序列交叉验证, 过拟合风险未量化。
3. **组合优化无约束** — 优化器产出不可交易的小仓位和极值权重。

## 五、落地进度 (test-v250)

| 优先级 | 功能 | 状态 | 文件 |
|--------|------|------|------|
| P1-1 | 因子相关性去冗余 (IC-rank ρ) | ✅ | `attribution.py` Step A.6, `state_manager.py` +FACTOR_REDUNDANT |
| P1-2 | crowdedness swallow-error fix | ✅ | `crowdedness.py` log warning instead of silent pass |
| P1-3 | 组合 min_weight 过滤 | ✅ | `portfolio.py` _apply_min_weight, config optimizer.min_weight=0.01 |
| P2-1 | IC 衰减曲线 | ✅ | `evaluation/factor_diagnostics.py` ic_decay_curve() |
| P2-2 | 分位数回测 | ✅ | `evaluation/factor_diagnostics.py` quantile_returns() |
| P2-3 | SHAP 可解释性 | ✅ | `qlib_model.py` shap_explain() + feature_importance() |
| P2-5 | TCA 交易成本分析 | ✅ | `execution/tca.py` analyze_execution() |
| P2-4 | 回测 Tear Sheet | P4 | 后续补充 |
| P2-6 | 滚动训练 CV | P4 | 后续补充 |
| P3 | 多模型集成 | P4 | 后续补充 |
| P3 | 多频数据因子 | P4 | 需数据层扩展 |
| P4 | 另类数据/Level-2 | P4 | 需外部数据源 |
