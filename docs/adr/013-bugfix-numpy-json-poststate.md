---
adr: 013
date: 2026-07-05
status: accepted
---

# ADR 013: Bugfix — numpy.int64 JSON 序列化 + _post_state 重试优化 + 清理

## 背景

全量代码 review 中发现 4 个问题。其中 Bug 1（仅 2 因子激活）经查阅 ADR 007 / eval_stepwise.sh / backtest.py 后确认为 Grinold & Kahn 步进评估的正确输出，非 bug。其余 4 项修复如下。

## 问题与修复

### Bug A (CRITICAL): numpy.int64 JSON 序列化崩溃

**根因**: `TargetPortfolio.positions` 属性返回 `(self.lots > 0).sum()` — pandas Series 的 `.sum()` 返回 `numpy.int64`，Python 3.14 的 `simplejson` 无法序列化此类型。

**影响**: 每次 rebalance 的 `_post_state` 调用全部崩溃（24 次 × 5 步骤 = 120 次静默失败）。

**修复** (两层防御):
1. [optimizer/portfolio.py:24](optimizer/portfolio.py:24) — `return int((self.lots > 0).sum())` 根因修复
2. [pipeline.py:34-48](pipeline.py:34) — 新增 `_sanitize_for_json()` 递归转换所有 numpy 类型

### Bug B (MEDIUM): _post_state 服务器离线时浪费重试时间

**问题**: `_post_state_sync()` 对所有 `requests.RequestException`（包括 `ConnectionError`）做 3 次指数退避重试。服务器未启动时，每个 pipeline 步骤浪费 ~6 秒。

**修复**:
- `requests.ConnectionError` → 立即返回，不重试（非瞬态错误）
- HTTP 4xx → 不重试（客户端错误）
- HTTP 5xx / `Timeout` / 其他 → 继续重试（瞬态错误）

### Bug C (LOW): config.yaml 残留旧阈值

`min_abs_ic: 0.02` 和 `min_ic_ir: 0.1` 是 P11 之前的固定阈值逻辑。ADR 007 已改为 t-test，代码不再读取这些配置。加 `# DEPRECATED` 注释。

### Bug D (LOW): 空表清理

`meta` 和 `fund_flow` 表 0 行且无写入计划，按模板 7 约束删除。`daily_basic` 虽有写入路径但同步失败（Tushare 限流），保留。
 (2026-07-05 u66f4u65b0: baostock u670du52a1u4e0du53efu7528uff0cdaily_basic.py u6807u8bb0u4e3a DEPRECATEDuff1bPE/PB u6570u636eu5b9eu9645u6765u81ea daily_valuation u800cu975e daily_basic)

### 非 Bug: 仅 2 因子激活

经查阅 ADR 007 + eval_stepwise.sh + backtest.py 后确认：Grinold & Kahn 三层评估正确执行，步进回测 legitimately 淘汰了 size/bp_ratio/gap_5d。2-factor 结果是方法论的正确输出，不是 bug。

## 附加: SKILL.md 新增 Bug 溯源规则

```
代码审查发现 bug 时，必须立即查阅所有关联文档（ADRs、相关脚本 git log、历史讨论），
全盘分析变更链后再做出判断。禁止仅凭代码片段下结论。
```

## 影响

- `_post_state` 不再因 numpy.int64 崩溃
- 服务器离线时 pipeline 速度提升 ~2 分钟/回测
- 配置文件无歧义残留
- 空表清理
