# ADR-042: 拒绝纳入的架构/算法建议（2026-07-31 审计）

## 状态
已决（所有建议均被拒绝）

## 背景
2026-07-31 对系统进行了全面的架构/理论差距审计，评估了数据库中碎片化、单点耦合、全局单例、测试覆盖等 7 项架构建议，以及遗传编程、Black-Litterman 等 17 项算法建议。全部基于 **¥5,000 资本、2-5 只持仓、单 MacBook 部署** 的约束条件逐项分析。

---

## 一、架构建议（7 项，全部拒绝）

### 1. 数据库碎片化 → 统一时序数据库

**建议**: 用 DolphinDB/ClickHouse/TimescaleDB + Feast 替代 SQLite × 5 + gzip CSV

**拒绝理由**:
- market.db 870 万行日线，SQLite 完全胜任
- SQLite 按域分拆（market/trades/metrics/backtest）是正确的架构选择
- 引入时序数据库增加运维复杂度，零功能收益

---

### 2. 单点耦合 pipeline → 微服务/事件总线

**建议**: pipeline.py 按层拆为独立服务，通过消息总线通信

**拒绝理由**:
- pipeline 本身就是顺序执行的数据流，拆开只会增加延迟
- 单账户系统不需要微服务架构
- 当前 orchestrator + subprocess 已实现子进程隔离

---

### 3. 全局单例过多 → 依赖注入容器

**建议**: broker、metrics、adapter 改为 DI 容器管理

**拒绝理由**:
- 每个组件只有一个实例是正确行为（单账户、单策略）
- DI 容器对单人系统是过度工程
- 测试时通过 monkeypatch 即可替换

---

### 4. Import 时副作用 → 延迟校验

**建议**: config/loader.py 的 `validate()` 不应在 import 时执行

**拒绝理由**:
- 已有 `QUANT_SKIP_CONFIG_VALIDATE=1` 环境变量跳过
- Import 时验证确保错误配置尽早暴露
- 设计正确，无需修改

---

### 5. Web 与调度同进程 → 独立部署

**建议**: Flask dev server + 后台线程分离

**拒绝理由**:
- orchestrator 和 evening_chain 已通过 subprocess 独立运行，不是线程
- Flask 崩溃不影响调度
- gunicorn/uvicorn 增加运维复杂度，无功能收益

---

### 6. 缺乏领域边界 → Hexagonal/Clean Architecture

**建议**: 按 core/application/infrastructure/interface 重构

**拒绝理由**:
- 核心模块（pipeline/execution/risk）无循环依赖
- 延迟 import 已解决耦合
- 对 ¥5,000 账户体量，过度架构是浪费

---

### 7. 测试覆盖不足 → 集成测试/行情回放

**建议**: 增加集成测试、mock 券商、行情回放

**拒绝理由**:
- ¥5,000 实盘本身就是最好的测试
- mock 券商/行情回放的人工成本远超收益
- 回测已覆盖核心逻辑

---

## 二、算法建议（17 项，全部拒绝）

### 因子挖掘

| 算法 | 拒绝理由 |
|------|---------|
| 遗传编程（gplearn） | 随机搜索，计算量爆炸（5000 股 × N 代），结果不稳定 |
| 强化学习因子搜索 | 状态空间巨大，无可靠奖励信号 |
| 图神经网络（供应链网络） | 供应链/股权网络数据不公开，无法获取 |

### Alpha 合成

| 算法 | 拒绝理由 |
|------|---------|
| 在线学习（EG/OGD） | 周频调仓，样本太少（~50 周/年），无法学习 |
| Bayesian Model Averaging | 需要多个独立模型，当前只有一个 LGB |
| Stacking | 同上，需要多模型集成 |
| Transformer 截面模型 | 5000 股自注意力，显存不够（16GB MacBook） |

### 风险模型

| 算法 | 拒绝理由 |
|------|---------|
| BARRA USE4 | 2-5 只持仓，看一眼就知道风险来源 |
| PCA/RMT 噪声剔除 | 协方差矩阵很小，Ledoit-Wolf 已充分 |
| GARCH-DCC 动态相关 | 2 只股票的相关性不需要动态建模 |
| Copula ES | 持仓太少，尾部依赖模型无意义 |

### 组合优化

| 算法 | 拒绝理由 |
|------|---------|
| Black-Litterman | Nano 层用等权/排名，不走均值-方差 |
| Robust Optimization | 需要大量历史场景数据 |
| CVaR Optimization | Nano 层优化器已足够 |
| Entropy Pooling | 无可量化主观观点输入 |

### 执行算法

| 算法 | 拒绝理由 |
|------|---------|
| Almgren-Chriss 最优清算 | 2 手单没有市场冲击，限价单足够 |
| Kissell 市场冲击 | 同上 |
| POV/TWAP/VWAP | 不需要拆单 |
| 强化学习执行 | 没有高频环境 |

### 时间序列

| 算法 | 拒绝理由 |
|------|---------|
| 状态空间模型 | 当前 HMM 3 状态已满足需求 |
| Particle Filter | 复杂度远超收益 |
| Online Bayesian Changepoint | 周频数据变化缓慢 |
| Regime-Switching GARCH | HMM + 简单波动率已够 |

### 另类数据

| 算法 | 拒绝理由 |
|------|---------|
| NLP 情感（BERT/LLM） | 没有实时新闻数据源 |
| 卫星/供应链 | 数据不公开且昂贵 |
| ESG 因子 | A 股 ESG 数据质量差 |

### 可解释性

| 算法 | 判断 |
|------|------|
| SHAP | ✅ 已实现 |
| LIME | ❌ 对树模型意义不大 |
| 注意力权重归因 | ❌ 没有 Transformer 模型 |

---

## 三、区间内差异审计（6 → 0 项可做）

对比回测与实盘的 6 项差异：

| # | 差异 | 判断 |
|---|------|:--:|
| 1 | 成交假设（回测开盘价 vs 实盘限价单） | 日频回测的自然局限 |
| 2 | 成本模型不一致 | ✅ test-v307 已修复 |
| 3 | 行情源字段硬编码 | 已有双源备援+防御检查 |
| 4 | 熔断/封板处理差异 | 日频回测的自然局限 |
| 5 | Broker 行为差异 | SimulatedAdapter 可用，无需真实券商 |
| 6 | T+1 资金可用性 | 卖出资金当天可用于买入，代码正确 |

---

## 四、采纳的改进（本次审计实际产出）

| 版本 | 内容 |
|------|------|
| test-v307 | 8 个已确认 bug（CostModel/monitor/combine_mode/var/universe/Order/turnover/limit_up） |
| test-v308 | 6 个隐性 bug（construct/config/import/flush/VaR/engine） |
| test-v309 | regime 动态仓位管理（牛 100%/震荡 60%/熊 30%） |
| test-v310 | 界面显示市场状态 + 仓位乘数 |
| test-v311 | state_broker 初始化检测 regime |
| test-v312 | hmmlearn 依赖修复 |
| test-v313 | ATR 止盈止损峰值持久化到 position_meta |
| test-v314 | 消除全部 30+ 处 except:pass 隐性 fallback |

---

## 决策原则（供后续审计引用）

> 任何架构/算法建议必须通过以下 3 个测试：
> 1. 对 ¥5,000 账户有可量化的收益提升
> 2. 实现复杂度不高于当前系统
> 3. 有可靠的数据源支撑
>
> 不满足任意一条 → 拒绝

---

## 补充：因子评估阈值锁定（2026-07-31）

评估阈值已对齐业界底线，**禁止再降**：

| 阈值 | 当前值 | 来源 | 底线 |
|------|--------|------|:--:|
| min_abs_ic | 0.02 | 券商共识底线 | ✅ 已到底 |
| min_icir | 0.25 | 聚宽/米筐/BigQuant A股 0.2-0.3 | ✅ 已到底 |
| monitoring_min_icir | 0.20 | ADR-041 语义变更后补调 | ✅ 已到底 |

**演变历史**：
- ADR-026 原始：ICIR ≥ 0.5（明汯/灵均标准）
- 后续下调：ICIR ≥ 0.25（聚宽/米筐国内平台 A 股范围）
- test-v285：monitoring ICIR 0.15→0.20（probation 参与信号后收紧）

**决策**：0 active 不是阈值问题，是因子在当前市场下确实未达机构级统计显著性。禁止以"放宽门槛"作为解决方案。修正方向应在因子质量（新增因子、curator 评估），而非降门槛。

---

*ADR-042 / 2026-07-31 / 基于 ¥5,000 Nano 层约束*
