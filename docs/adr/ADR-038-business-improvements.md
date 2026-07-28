# ADR-038: 业务链路改进 7 项 (ADR-037 后续)

**日期**: 2026-07-28
**状态**: ✅ 已实施
**依赖**: ADR-037 (因子计算审计修复)

---

## 改进清单

### 🥇 1. 冷却期过滤提前到信号生成阶段

**文件**: `quant/scheduler/signals.py`

**问题**: 冷却过滤只在 `ExecutionModel.run()` 执行阶段，冷却标的仍出现在 `daily_signals` 和 Web UI 候选池中。

**修复**: `scheduler/signals.py` 在调用 `generate_signals()` 前通过 `RiskManager.get_cooloff_symbols()` 查询冷却列表，传入 `exclude_symbols` 参数。

---

### 🥇 2. LightGBM 夜间链自动重训

**文件**: `quant/scheduler/lgb_train.py` (NEW), `quant/scheduler/orchestrator.py`

**内容**:
- 新增 `lgb_train` 调度任务，factor_cache 完成后触发
- 仅周一/周四执行（因子值不变时重训无增量价值）
- 超时 30 分钟，重试上限 2 次
- `_check_lightgbm()` 检测 → lightgbm 未安装自动跳过

---

### 🥈 3. 因子冗余同向检测

**文件**: `quant/scheduler/attribution.py`

**内容**: 新增 G5 步骤 — 检测同一方向高相关因子对（如 momentum_5d ≈ reversal_5d）。
- 遍历所有 using 因子，检查 IC 同号 + |ρ| > 0.85 的对
- 发现冗余对 → log WARNING + metrics counter
- 异常时 skip（不影响其他归因步骤）

---

### 🥈 4. vnpy 模拟盘验证 (ADR-036 延续)

**状态**: 代码完备，等待 vnpy 安装环境

- `BrokerAdapter` injection 修复（`scheduler/execute.py` 顺序调整）
- 安装 vnpy 后修改 `execution.broker.adapter: "vnpy_ctp"` 即可

---

### 🥈 5. Web 仪表盘 LGB 模型状态面板

**文件**: `web/app.py`, `web/templates/index.html`, `web/static/app.js`

**内容**:
- 新增 `/api/lgb` 端点：返回 lightgbm 可用性/训练状态/模型元数据
- 概览页新增 LGB 面板：状态指示灯 + IC/样本数/特征数 KPI
- 未安装 → "未安装，pip install lightgbm"
- 未训练 → "未训练，run train_lgb_model()"
- 已训练 → 绿色指示灯 + 训练 IC + 样本数

---

### 🥉 6. 回测结果 DB 持久化

**文件**: `quant/backtest/loop.py`

**内容**: 回测完成后自动写入 `backtest_trades.db` 的 `backtest_runs` 表。
- 字段: strategy, start/end date, capital, 全部 metrics (Sharpe/CAGR/MDD/Sortino/Calmar/Alpha/IR/Beta), diagnosis JSON
- 幂等: `INSERT OR REPLACE` (同 strategy+started_at 去重)

---

### 🥉 7. 信号质量对比 API

**文件**: `web/app.py`

**内容**: 新增 `/api/signals/quality` 端点
- 今日信号: date/count/avg_score/max_score
- 历史信号: avg_count/avg_score/n_days (最近 20 天)
- 对比: count_pct (今日 vs 历史偏差%), score_diff

---

## 变更清单

| 文件 | 变更 |
|---|---|
| `quant/scheduler/signals.py` | 冷却期过滤提前 |
| `quant/scheduler/lgb_train.py` | NEW: LGB 夜间重训调度 |
| `quant/scheduler/orchestrator.py` | +lgb_train 任务 + timeout |
| `quant/scheduler/attribution.py` | +G5 因子冗余检测 |
| `quant/scheduler/execute.py` | BrokerAdapter 注入顺序修复 |
| `quant/backtest/loop.py` | +回测结果 DB 持久化 |
| `web/app.py` | +`/api/lgb` + `/api/signals/quality` |
| `web/templates/index.html` | +LGB 模型状态面板 |
| `web/static/app.js` | +renderLGB() + poll LGB |

## 测试结果

- 221 个测试全部通过 ✅
- Web 服务重启成功，`/api/lgb`、`/api/signals/quality` 端点正常 ✅
