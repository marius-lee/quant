# 代码逻辑问题修复 — 2026-05-30

基于全面审查报告第四部分（代码逻辑问题 4.1-4.4）逐项修复。

---

## 4.1 异常静默吞没

| 文件 | 位置 | 修复前 | 修复后 |
|------|------|--------|--------|
| `engine/predictor.py:28` | 分批预测异常 | `except Exception: continue` | `except Exception: logger.warning(...); continue` |
| `engine/backtest_runner.py:33` | 回测某日预测失败 | `except Exception: return pd.Series(...)` | `except Exception: logger.warning(...); return pd.Series(...)` |
| `auto_run.py:115` | 解析历史推荐 | `except: pass` | `except Exception: logger.warning(...); return set()` |

注：`auto_run.py` 此处在 3.2 中已顺带修复。

## 4.2 IC 计算脆弱类型判断

**文件:** `factor/screening.py:56-57`

`ics.mean()` 返回类型因 pandas 版本/数据维度而异（Series 或标量），原代码用 `hasattr(ics.mean(), 'iloc')` 检测。改为先缓存到局部变量再检测，逻辑一致但更清晰。

## 4.3 game_theory 列构造冗余

**文件:** `factor/game_theory.py:40-42`

原代码在每个窗口迭代内嵌套遍历 `close.columns`（`len(windows) × len(factors) × len(stocks)` 次列名拼接）。改为窗口循环只收集因子名和数据，列 MultiIndex 在循环外一次性构建。

## 4.4 ensemble 训练异常无日志

**文件:** `strategy/ensemble.py:42`

LightGBM/XGBoost 等模型训练失败时原代码 `except Exception: return None`，无任何日志。添加 `logger.warning(f"model {name} training failed, excluding from ensemble")`。
