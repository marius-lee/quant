# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。

---

## test-v212: Alpha 候选池 UI 优化

**变更**: `web/static/app.js`, `web/static/style.css`

- renderSignals: 候选池从 8 行缩至 5 行，得分从 3 位小数缩至 2 位
- reason 列截断：超过 2 个因子贡献时只显示前 2 个 + "N more"，hover 显示完整
- CSS: `.status-badge` → `.badge` 基类重命名，对齐 JS 中的 `class="badge badge-red"`
- 新增 `.trunc-reason em` 样式


---

## test-v213: execute 跳过二次资金裁剪

**Bug**: pipeline 已分配 2 只股票，但 execute 的 compute_trades cash feasibility 检查做二次裁剪，
累积成本超现金时砍掉第二只。例: 001258 成本 ¥4,357 → 剩余 ¥643 < 600744 成本 ¥727 → 被裁剪。

**修复**: `rebalance.py` compute_trades 新增 `skip_cash_feasibility` 参数，execute 传入 True。
pipeline 已在信号生成阶段完成分配，execute 仅执行 delta。

**变更文件**: `quant/optimizer/rebalance.py`, `quant/scheduler/execute.py`
