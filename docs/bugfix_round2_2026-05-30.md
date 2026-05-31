# 第二轮 Bug 修复 — 2026-05-30

基于第二轮全面审查报告的 3 个关键 bug + 3 个中等问题逐项修复。

---

## P0: real_fundamental NULL 值崩溃

**文件:** `factor/real_fundamental.py:29-41`

基本面同步未覆盖的股票在 stocks 表中 PE/PB/市值/52周高低为 NULL（Python None），导致 `None > 0`、`max(None, 1)`、`None - None` 引发 TypeError。

**修复:** 用 `or 0` / `or 1` 在计算前将所有 NULL 值转为安全默认值。

## P0: 滚动重训练前视偏差

**文件:** `engine/backtest_runner.py:55`

`close_df.pct_change(5).shift(-5)` 使用包含未来日期的完整 close_df，重训练时窗口末尾的样本会使用不可知的未来价格计算目标收益。

**修复:** `close_df.loc[:date]` — 只用截至当前测试日期的数据计算目标收益。同时简化了重训练触发条件（用 `last_retrain_idx` 整数替代复杂的列表容器 + 嵌套条件）。

## P1: 因子缓存日期格式不匹配

**文件:** `factor/cache.py:28-34`

daily 表日期格式为 `YYYYMMDD`（tushare），factors_cache 表为 `YYYY-MM-DD`（pandas）。字符串比较 `"2024-05-30" >= "20240530"` 始终为 False（`-` ASCII 45 < `0` ASCII 48），导致每次运行都全量重建因子。

**修复:** `fc_max = fc_max.replace("-", "")` 统一为 YYYYMMDD 后再比较。

## P1: Scaler 数据泄露

**文件:** `strategy/ensemble.py:30`

`self.scaler.fit_transform(X)` 在全量数据上拟合，然后才划分训练/验证集。验证集的均值和方差泄露进 scaler 参数，导致验证 IC 虚高。

**修复:** 先划分，再 `fit_transform(X_tr_raw)` + `transform(X_va_raw)`。

## P1: 回测信号异常静默

**文件:** `backtest/event_engine.py:57-59`

`signal_fn(date)` 异常被 `except Exception` 静默吞掉，回测结果为空仓但开发者完全不知道。

**修复:** 添加 `logger.warning(f"signal_fn failed on {date}, using zero weights")`。

## P2: 空表边界崩溃

**文件:** `factor/cache.py:34`

daily 表为空时 `dl_max = None`，`fc_max >= dl_max` 触发 `TypeError: '>=' not supported between instances of 'str' and 'NoneType'`。

**修复:** `if dl_max is None: dl_max = "00000000"` 添加 None 保护。
