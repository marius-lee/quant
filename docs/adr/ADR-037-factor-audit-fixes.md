# ADR-037: 因子计算审计修复

**日期**: 2026-07-28
**状态**: ✅ 已实施
**依赖**: ADR-035 (基础设施替换分析)

---

## 审计范围

通读全部因子计算代码（~3500行），对标准确性、鲁棒性、文献合规性进行全面排查。

## 修复项

### 🔴 修复一：截面 z-score 增加 winsorize + MAD 标准化

**文件**: `quant/factor/registry.py` — `_cs_zscore()`

**问题**: 原有实现为纯 mean/std z-score，对极端值（单日涨跌停）敏感。单只股票的 ±10% 涨停会拉偏全截面均值和标准差，导致其余 5000+ 股票的因子值集体失真。

**修复**:
1. Winsorize 1%/99% 分位裁剪极端值（`config factor.compute.winsorize_pct: 0.01`）
2. MAD（中位数绝对偏差）标准化替代均值/std
3. MAD=0 时（稀疏因子如 zt_streak）自动回退到 winsorized mean/std

**来源**: Barra USE4 → MAD 标准化；Qlib/WorldQuant → winsorize 后标准化。

---

### 🟡 修复二：动量/反转因子方向注释统一

**文件**: `quant/factor/compute/price/_momentum.py`

**问题**: P0-4 数据清洗后，IC 检验发现 A 股反转效应不成立，两个因子都改为 `+cum` 方向（正向动量）。但命名仍叫 `reversal`，注释内部分歧。

**修复**: 统一注释，明确方向决策来源，标注 TODO 待下一轮因子评估重新检验反转方向。`reversal_5d` 与 `momentum_5d` 目前高度相关是已知问题，等待数据验证后决定是否退役或改造。

---

### 🟡 修复三：隔夜缺口注释自相矛盾

**文件**: `quant/factor/compute/price/_momentum.py` — `compute_overnight_gap()`

**问题**: docstring 说"取负号使负缺口得高分"，代码实际用 `+avg_gap`，行尾注释又说"正缺口(高开)→强势→高分"。三处注释给出了两个相反的方向。

**修复**: 统一为 `+avg_gap` 方向（正缺口=高开=高分），清理全部矛盾注释。IC 实证确认正向（IC≈0.03-0.04）。

---

### 🟡 修复四：基本面因子季报真空期衰减

**文件**: `quant/factor/compute/_dispatch.py`

**问题**: 季报在 4/8/10 月底发布后的真空期（5-7月、11-3月），基本面因子值不变，但其预测力随时间衰减。

**修复**: 在 `compute_all_factors()` 的基本面因子计算完成后，对每个因子按距最近财报天数做指数衰减：
```
value' = value × exp(-λ × days_since_report)
λ = ln(2)/90 ≈ 0.0077 → 半衰期 90 天
```
30天内（刚发布）不衰减。通过 `config factor.compute.earnings_decay_lambda` 控制。

---

### 🎨 终端显示修复

**文件**: `~/.pi/agent/themes/quant-dark.json`

**问题**: pi dark 主题中 `text`、`userMessageText`、`toolTitle` 等设为 `""`（终端默认色），某些终端默认黑色→黑底黑字不可读。

**修复**: 创建 `quant-dark` 主题，所有文本色显式设为亮色：
- `text`: #D4D4D4, `userMessageText`: #ECECF1
- `toolTitle`/`toolOutput`: 显式亮色
- accent: #F2964A（与仪表盘品牌色一致）

已写入 `~/.pi/agent/settings.json` 启用。

---

## 测试结果

- 221 个测试全部通过 ✅
- 新增 winsorize 测试用例 ✅
- MAD fallback 稀疏因子兼容性验证 ✅

## 变更清单

| 文件 | 变更 |
|---|---|
| `quant/factor/registry.py` | `_cs_zscore()` 重构：winsorize + MAD + fallback |
| `quant/factor/compute/price/_momentum.py` | 动量/反转/隔夜缺口注释统一 |
| `quant/factor/compute/_dispatch.py` | 基本面因子季报真空期衰减 + `import numpy, _require_cfg` |
| `quant/config/config.yaml` | +`factor.compute.winsorize_pct`, +`earnings_decay_lambda` |
| `test/test_factor_compute.py` | 适配 MAD 标准化新语义 |
| `~/.pi/agent/themes/quant-dark.json` | 终端主题 (显式亮色文本) |
| `~/.pi/agent/settings.json` | `theme: "quant-dark"` |
