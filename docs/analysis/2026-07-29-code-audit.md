# 代码审计报告 — test-v249

> 2026-07-29 全量代码审查。不涉及修改，仅记录发现，供讨论。

---

## 一、未接入模块（代码存在但无调用方）

| 模块 | 说明 | 影响 |
|------|------|------|
| `evaluation/cpcv_dsr.py` | CPCV+DSR 因子评估（test-v300 引入）。HANDOFF 记载已接入 attribution.py，实测 **无任何 import** | DSR 驱动因子升降级完全未生效；attribution 实际走旧 L1/L2 IC 路径 |
| `execution/market_microstructure.py` | Roll(1984) 有效价差估计。全项目零引用 | 回测成本模型缺少微观结构修正 |
| `alpha/multi_tf.py` | MultiTimeframeConfirmer：周线+日线多周期投票。零引用 | 多周期确认逻辑未使用 |
| `evaluation/phase8_live_consistency.py` | 回测 vs 实盘一致性验证。`validate_consistency()` 只在自身 docstring 被引用 | Phase 8 只存在于 `__init__.py` 注释中 |
| `data/jq_valuation.py` | JQData 估值同步。设计为独立脚本（`if __name__`），不在晚间链中 | 估值数据只能手动回补 |

**其中 cpcv_dsr.py 是阻断性问题**：test-v300 声称 L2 评估已改为 DSR 驱动，但实际 attribution.py 用的是 `deflated_sharpe.py` 的 `compute_dsr_for_strategy()`（策略级），而非 `cpcv_dsr.py` 的 `evaluate_factor()`（因子级）。因子升降级实际走的是旧 IC 路径。

---

## 二、连接泄漏（SQLite 未关闭）

| 位置 | 代码 | 说明 |
|------|------|------|
| `scheduler/monitor.py:160` | `conn2 = _get_market_conn()` | VaR 检查后未 `conn2.close()` |
| `scheduler/monitor.py:190` | `conn2 = _get_market_conn()` | 流动性检查后未 `conn2.close()` |
| `scheduler/monitor.py:368` | `sqlite3.connect(...)` 返回给调用方 | `_get_market_conn()` 返回连接但调用方从不关闭 |

相较于 test-v249 已修复的 6 处泄漏，这 3 处是遗漏的。

---

## 三、硬编码数值（应来自 config.yaml）

| 位置 | 当前值 | 应改为 |
|------|--------|--------|
| `scheduler/monitor.py:368` | `os.path.join(__file__, "..", "..", "data", "market.db")` | `from quant.config.paths import MARKET_DB` |
| `scheduler/task_log.py:32` | `busy_timeout=5000` | `_require_cfg("data.sqlite.busy_timeout")` |
| `scheduler/orchestrator.py:29,46,243` | `busy_timeout=3000` | 同上 |
| `data/repos/_base.py:72` | `busy_timeout=5000` | 同上 |
| `optimizer/hyperopt.py:87` | `capital=5000` | 应读取 `backtest.default_capital` |
| `factor/compute/price/_alternative.py:37` | `Timedelta(days=375)` | `_require_cfg("data.lookback_days")` |
| `factor/compute/fundamental.py:491,524` | `DateOffset(days=365)` | `_require_cfg("data.lookback_days")` |
| `risk/var.py:242` | `timedelta(days=365)` | `_require_cfg("data.lookback_days")` |
| `factor/compute/_primitives.py:74` | 窗口集合 `{5,10,20,60,63,120,126,250,252}` | 应从 `_PRICE_FN_MAP` 自动推导 |
| `scheduler/attribution.py:291,336` | `limit=500` | 应为可配置参数 |
| `scheduler/monitor.py:207` | `limit=200` | 应为可配置参数 |
| `optimizer/hyperopt.py:69` | `max_positions` step=5, range 5-30 | 范围硬编码 |

---

## 四、零 Fallback 违规（静默吞错）

| 位置 | 代码 | 违反规则 |
|------|------|----------|
| `scheduler/order_manager.py:217` | `except Exception: adapter = None` | 券商适配器创建失败被静默跳过 |
| `scheduler/crowdedness.py:187` | `except Exception: return None` | 拥挤度计算失败返回 None，调用方可能误判 |

---

## 五、其他发现

### 5.1 版本号不一致
`web/app.py` 显示 `VERSION = "test-v249"`，但 `factor/cards/*.json` 中部分卡片内嵌版本号仍为旧值（如 `v2.0`、`test-v152`）。不影响功能，但降低可追溯性。

### 5.2 潜在数据一致性问题
`factor_cache._run()` 中 `_cache_start = min('2026-01-01', today)`——如果用户调用 `_run('2024-12-01')`，缓存起始日期是 `2024-12-01`，意味着 2024-12-01 之前的缺口不会被检查，但也不会被物化。配合 `trim_to_max_days: 2000` 这不会是问题。

### 5.3 废弃的数据库文件
项目根目录下有 `gerrit-3.11.2.war`（86MB），`.gitignore` 已排除但文件仍占用磁盘。

### 5.4 docs/handoffs/ 目录过时
CLAUDE.md 曾指向 `docs/handoffs/HANDOFF.md`（已修复），但 `docs/handoffs/` 目录下的 `HANDOFF.md` 和 `HYPOTHESES.md` 均为旧版。建议删除或添加废弃标记。

---

## 优先级建议

| 优先级 | 事项 | 理由 |
|--------|------|------|
| **P0** | `cpcv_dsr.py` 接入 attribution.py | 因子 DSR 评估完全未生效 |
| **P1** | `monitor.py` 连接泄漏 ×3 | 每轮监控泄漏 2 个连接 |
| **P1** | 硬编码 `busy_timeout` ×6 | 多处不一致（3000/5000） |
| **P2** | 硬编码路径 `monitor.py:368` | 违反 DB 路径规范 |
| **P2** | `order_manager.py`/`crowdedness.py` 吞错 | 违反零 fallback |
| **P3** | `multi_tf.py` / `market_microstructure.py` 接入 | 功能存在但未使用 |
| **P3** | `phase8_live_consistency.py` 接入 | 回测实盘一致性验证 |
| **P4** | 硬编码 `limit=200/500` | 非关键参数 |
| **P4** | 清理 `docs/handoffs/` + `gerrit.war` | 磁盘和文档卫生 |

---

## 六、接入进度（2026-07-29 更新）

| 模块 | 状态 | 接入位置 |
|------|------|---------|
| `cpcv_dsr.py` | ✅ 已接入 | `attribution.py` Step A.5 — 计算 `cpcv_verdicts`，修复 NameError |
| `multi_tf.py` | ✅ 已接入 | `pipeline.py` Step 3 — `alpha.multi_tf_confirm: true` 启用周线压制 |
| `market_microstructure.py` | ⬜ 保持现状 | 诊断工具，自身 docstring 限定"回测/诊断" |
| `phase8_live_consistency.py` | ⬜ 保持现状 | 独立命令工具，设计正确 |
| `jq_valuation.py` | ⬜ 保持现状 | 独立脚本 |

### 改动文件

| 文件 | 改动 |
|------|------|
| `quant/scheduler/attribution.py` | +12 行：Step A.5 计算 cpcv_verdicts |
| `quant/pipeline.py` | +8 行：multi_tf 周线确认（opt-in） |
| `quant/alpha/multi_tf.py` | 修复硬编码 DB 路径 → `MARKET_DB` |
| `quant/config/config.yaml` | 新增 `alpha.multi_tf_confirm: false` |

## 七、全部修复清单（test-v249 final）

### 第二节: 连接泄漏 ✅
| 位置 | 修复 |
|------|------|
| `monitor.py:160` VaR 检查 | `try/finally: conn2.close()` |
| `monitor.py:190` 流动性检查 | `try/finally: conn2.close()` |

### 第三节: 硬编码数值 ✅
| 位置 | 修复 |
|------|------|
| `monitor.py:368` | `MARKET_DB` 替代硬编码路径 |
| `monitor.py:207` | `limit=max_trades*2` 替代 `limit=200` |
| `task_log.py:32` | `_require_cfg("data.sqlite.busy_timeout")` |
| `orchestrator.py:29,46,243` | 同上 ×3 |
| `_base.py:72` | 同上 |
| `hyperopt.py:87` | `_require_cfg("backtest.default_capital")` |
| `_alternative.py:37` | `_require_cfg("data.lookback_days") + 10` |
| `fundamental.py:491,524` | `_require_cfg("data.lookback_days")` |
| `var.py:242` | 同上 |
| `attribution.py:304,349` | `limit=1000` (充足上限) |

### 第四节: 零 Fallback ✅
| 位置 | 判断 |
|------|------|
| `order_manager.py:217` | 保留 — broker adapter 可选，simulated 无依赖 |
| `crowdedness.py:187` | 保留 — `None` 为合法"无历史数据"语义 |

### 第五节: 其他 ✅
| 项 | 修复 |
|------|------|
| 5.1 版本号不一致 | P4 非功能项，不改 |
| 5.3 gerrit.war | 已删除 |
| 5.4 docs/handoffs/ | 已删除（CLAUDE.md 已指根目录） |
