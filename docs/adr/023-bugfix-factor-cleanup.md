# ADR 023 — Bug修复: 因子计算管道清理 (2026-07-06)

## 背景

全量评估时发现多个故障模式:
1. `amihud_250d` IC=0.0000 — compute_min_valid 要求 125 点, 但 eval 只加载 ~117 点
2. `ma_alignment_20d` 报错 "takes 2 positional arguments but 3 were given"
3. `hsgt_flow_5d` 报错 "cannot import name 'get_northbound_flow'"
4. `volatility_20d` / `idio_vol_20d` / `skewness_20d` / `amihud_20d` — 旧名残留, 与变体重名但窗口恒定相同, 纯噪声
5. `main_flow_ratio` — 注册在 DB 但不在任何 fn map → 死因子
6. `momentum_10d` — 已废弃, 被 63d/126d/252d 变体替代

## 修复项

### 1. compute_amihud min_valid 修正
**问题**: `min_valid = max(1, int(250*0.5)) = 125` 超过 eval 可用 117 天, 全 NaN → IC=0
**修复**: 用 `min(window, p_slice.shape[0])` 代替 `window` 计算 min_valid
**原理**: 数据不足时用实际可用数据长度代替理论窗口

### 2. compute_ma_alignment 参数签名
**问题**: 函数签名 `fn(data, date)` 缺少 window 参数, dispatch 调用 `fn(data, date, win)` 失败
**修复**: 添加 `window: int = 20` 参数

### 3. 遗漏默认值修正
**问题**: config load 失败时 `_VOLATILITY_WINDOW = 20`, 偏离 config.yaml 标准 126d
**修复**: fallback 改为 126, 同时修正 `_DOWNSIDE_VOL_WINDOW` 和 `_IDIO_VOL_WINDOW`

### 4. hsgt_flow_5d — 移除
**问题**: `get_northbound_flow` 不存在; 陆股通数据表未填充
**处理**: 从 factor_registry 和 _PRICE_FN_MAP 删除

### 5. main_flow_ratio — 移除  
**问题**: 函数存在但不在 _PRICE_FN_MAP → 永不计算
**处理**: 从 factor_registry 删除

### 6. 删除名实不符的旧因子名
从 _PRICE_FN_MAP 和 factor_registry 删除: `volatility_20d`, `idio_vol_20d`, `skewness_20d`, `amihud_20d`, `momentum_10d`
保留: `volatility_126d`, `idio_vol_126d`, `skewness_60d`, `amihud_250d`, `momentum_63d/126d/252d`

### 7. stats_cache.py 标签清理
删除 display_names/categories/sources 中的 `*_20d` 和 `momentum_10d` 条目

## 结果

- 因子数: 42 → 35 (registry ↔ fn maps 完全一致)
- 破损因子: 全灭
- amihud_250d: min_valid 自适应实际数据长度
- ma_alignment_20d: 不再崩溃
- 评估管道不再有 ImportError/ArgumentError 噪音
