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

## test-v215: 实盘执行全链路修复 — 涨停预检 + alpha 优先级裁剪 + 价格缓冲

**Phase A — execute.py 三处修复**:
1. `fetch_quotes` 加 `include_ask_bid=True` (获取五档盘口)
2. 新增 Step 3.5 涨停封死预检: 用 ask_volume==0 + 涨停价判断开盘封死, 跳过不挂单, 写入 exec_notes
3. 裁剪逻辑从「成本升序」改为「alpha 得分降序 + 实时开盘价重算股数」: top1 优先分配资金, 剩余给 top2

**Phase B — 价格缓冲**:
- pipeline 分配时预留 5% 价格波动空间 (Nano 层)
- config.yaml: `execution.price_buffer: 0.05`
- 用昨收价 × 1.05 估算成本, 减少 execute 阶段 reopen 价差导致的 validate_orders 裁剪

**变更文件**: `quant/scheduler/execute.py`, `quant/optimizer/portfolio.py`, `quant/config/config.yaml`
