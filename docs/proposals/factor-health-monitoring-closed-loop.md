# 因子健康监控闭环方案 — 业界标准对齐

> 生成: 2026-07-22 | 基于 G1 OOS Walk-Forward + 三级检测体系

---

## 一、业界标准参照

| 标准 | 来源 | 核心原则 |
|------|------|---------|
| IC = corr(forecast, return) | Grinold & Kahn (1999) Ch.6 | 因子预测力的唯一定量指标 |
| 每日截面 IC 滚动均值 + IR | MSCI/Barra 风险模型 | 因子收益每日计算，滚动 20 日 t-stat 低于 2 → 审查 |
| OOS/IS IR 比率 | WorldQuant | < 0.3 = 严重衰减，0.3-0.5 = 警告 |
| 三级检测体系 | AQR/Two Sigma | L1: 滚动 IC → L2: OOS/IS → L3: 稳定性校验 |
| 归一化关系表 | 全行业 | (date, factor, ic) 追加不可修改，不缓存衍生指标 |

---

## 二、目标数据模型

### 新建表：factor_ic_daily

```sql
CREATE TABLE factor_ic_daily (
    date        TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    ic_value    REAL,
    n_stocks    INTEGER,
    is_ir       REAL,
    oos_ir      REAL,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (date, factor_name)
);
```

每因子每天一行，追加不可修改。是因子绩效的**唯一真相源**。

### 保留/改造/删除清单

| 表/字段 | 决定 | 理由 |
|---------|------|------|
| `factor_ic_snapshot` | **删除** | JSON blob 不可查询，存静态假数据 |
| `factor_ic_daily` | **新建** | 归一化因子 IC 记录 |
| `factor_snapshot.ic_series` | **删除** | 三月未更新，由 factor_ic_daily 替代 |
| `factor_snapshot`（其余字段） | **保留** | UI 展示用（ic, ic_ir, factors, correlation） |
| `factor_registry.ic_mean` | **改为每日同步** | 从 factor_ic_daily 滚动均值写入 |

---

## 三、三级检测体系（AQR 标准）

```
Level 1: 滚动 IC 监控              数据源: factor_ic_daily
  └─ 最近 20 日 IC 均值 vs 当前值 → 偏离 >50% → degraded

Level 2: OOS/IS 比率               数据源: G1 OOS per_factor
  ├─ OOS_IR < 0            → immediate monitoring
  ├─ OOS_IR/IS_IR < 0.3    → monitoring（严重衰减）
  └─ OOS_IR/IS_IR > 0.5    → recovery_candidate

Level 3: 稳定性校验               数据源: factor_ic_daily
  ├─ 连续 N 天偏离 < 阈值  → 确认恢复 → active
  ├─ monitoring ≥ buffer   → retired
  └─ 其他                  → 保持 monitoring
```

---

## 四、逐文件修改

### 4.1 `factor_repo.py`
- 新建: `insert_ic_daily()`, `get_ic_rolling()`, `get_ic_rolling_all()`, `sync_ic_mean_to_registry()`
- 删除: `save_ic_snapshot()`, `get_recent_ic_snapshots()`, `delete_old_ic_snapshots()`
- DDL: `_ensure_tables()` 加 `factor_ic_daily`

### 4.2 `oos_verify.py`
- `run_oos_check()` 返回增加 `ic_daily` 字段（透出逐日 IC 序列）

### 4.3 `attribution.py`
- 删除 L106-222（static ic_mean 整块）
- 在 G1 之后新增统一评估块，执行 Level 1→2→3 三级检测 + 状态变更

### 4.4 `stats_cache.py`
- 删除 ic_series 写入（L280-281）
- factor_cache 结束后调用 sync_ic_mean_to_registry

### 4.5 `config.yaml`
- 新增: `oos_severe_decay: 0.0`, `oos_warning_decay: 0.3`, `oos_recovery_threshold: 0.5`, `ic_daily_retention_days: 252`
- 删除: `snapshot_keep_days`, `oos_warn_threshold`（语义被替代）

---

## 五、数据流闭环

```
19:00 daily_data → OHLCV 入库
20:00 attribution ──┬─ G1 OOS: 新鲜行情 → compute_all_factors → 逐日 Spearman IC
                    ├─ Step A: 写 factor_ic_daily
                    ├─ Step B: Level 1 滚动 IC 监控
                    ├─ Step C: Level 2 OOS 反转检测
                    ├─ Step D: Level 3 稳定性校验 → 执行状态变更
                    └─ Step E: 同步 ic_mean → factor_registry
21:00 factor_cache ──┬─ 全量因子计算 → factor_snapshot（不含 ic_series）
                     └─ 同步 ic_mean → factor_registry
```

---

## 六、迁移步骤

1. 建表: `factor_ic_daily` DDL
2. 扩 G1 返回: `oos_verify.py` 加 `ic_daily`
3. 重写归因块: `attribution.py` L106-222 → 新统一评估块
4. 删旧表: `DROP TABLE factor_ic_snapshot`
5. 清 ic_series: `stats_cache.py` 不再写
6. 加同步: `factor_cache.py` → `factor_registry.ic_mean`
7. 更新配置: `config.yaml`
8. 文档: CLAUDE.md, HANDOFF.md, DATA_DICTIONARY.md
