# 第三轮清理 — 2026-05-30

低优项清理 + 引入 bug 修复。

---

## IC 标准化一致性

**文件:** `factor/screening.py:45`

Y 轴标准化使用全局 `ys.mean()` 替代每日期 `ys.loc[dates]`，与 X 轴的每日期标准化不一致，导致 IC 值存在系统性缩放偏差。同时处理 NaN（单股票日期 std 返回 NaN）。

**修复:** Yz 改用 `ys.loc[dates].values` 逐日期标准化，并处理零值和 NaN。

## 异常日志补充

| 文件 | 修复 |
|------|------|
| `data/repository.py:87` | `max_date()` 异常加 `logger.warning` |
| `factor/alpha_factory.py:100` | 候选因子生成失败加 `logger.warning` |

## 死代码删除

| 文件 | 删除内容 |
|------|---------|
| `factor/demon.py:73-80` | `filter_demon_stocks()` — 定义未调用 |
| `factor/alpha_factory.py:148-158` | `generate_and_append()` — 定义未调用 |

注：`backtest/metrics.py` 的 `print_metrics()` 保留，作为交互式调试工具。

## 引入 Bug 修复

**文件:** `backtest/event_engine.py`

在 P1 修复中添加 `logger.warning(...)` 但未导入 logger，会导致 NameError。

**修复:** 添加 `from utils.logger import get_logger` + `logger = get_logger("backtest.engine")`。
