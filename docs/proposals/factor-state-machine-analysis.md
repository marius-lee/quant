# 因子状态机：现状分析 + 业界对标 + 改进方案

> 生成: 2026-07-18 | 基于全量代码审查

---

## 一、当前项目的因子状态

### 1.1 状态定义（7个有效状态 + 1个脚本状态）

| 状态 | 是否有效 | 定义 | 进入条件 | 退出条件 |
|------|---------|------|---------|---------|
| **`registered`** | ✅ | 因子已注册到`factor_registry`，等待首次评估 | 新因子INSERT时 | 诊断预筛通过→`candidate` |
| **`candidate`** | ✅ | 通过诊断预筛，进入Phase 2评估 | 诊断模块标记pass | Phase 2 pass→`active` / Phase 2 fail→`rejected` / 边缘→`monitoring` |
| **`active`** | ✅ | 通过全部Phase 2+3+4评估，参与实盘交易 | `sync_factor_status()` P2+P3+P4全过 | IC衰减→`monitoring` |
| **`monitoring`** | ✅ | 🔴 **双义**：① IC边缘（Phase 2 marginal）② IC衰减（active降级）③ 数据不足待验 | 多种路径 | IC恢复→`active` / 持续衰减→`retired` |
| **`retired`** | ✅ | 持续衰减超过buffer天数（默认10天），永久退役 | `monitoring`持续衰减≥buffer | 无（当前无恢复路径） |
| **`rejected`** | ✅ | Phase 2/3/4失败（IC/ICIR/CPCV/成本不达标） | `sync_factor_status()` | 无（当前无恢复路径） |
| **`backtesting`** | ⚠️ | 虚拟状态——不是实际状态值，是`_resolve_statuses()`的过滤标签，等价于`(registered, candidate, retired)`的并集 | N/A | N/A |
| **`inactive`** | ❌ | `build_fin_factors.sh`脚本中INSERT时使用，但**不在VALID_STATUSES中** | 手动SQL INSERT | 手动UPDATE |

### 1.2 VALID_STATUSES 源码

```python
# quant/data/repos/factor_repo.py:13
VALID_STATUSES = frozenset({"registered", "candidate", "active", "monitoring",
                             "retired", "rejected", "backtesting"})
```

### 1.3 状态解析逻辑（`_resolve_statuses`）

```python
# quant/factor/compute/_registry.py:8-17
if status_filter == 'using':
    return ('active',)          # 实盘交易: 只用 active
if status_filter == 'backtesting':
    return ('registered', 'candidate', 'retired')  # 评估候选池
```

### 1.4 完整流转图（当前）

```
                    ┌─────────┐
                    │inactive │  ← 脚本状态, 不在VALID_STATUSES!
                    └────┬────┘
                         │ 诊断预筛通过
                    ┌────▼────┐
                    │registered│  ← 初始注册
                    └────┬────┘
                         │ 诊断预筛 pass
                    ┌────▼────┐
                    │candidate │  ← 等待 Phase 2
                    └────┬────┘
                         │ Phase 2+3+4
              ┌──────────┼──────────┐
              │ pass     │ marginal │ fail (IC/ICIR 全不达标)
         ┌────▼───┐ ┌───▼────┐ ┌───▼──────┐
         │ active │ │monitoring│ │ rejected  │
         └────┬───┘ └───┬────┘ └──────────┘
              │          │              无恢复路径 ⚠️
    IC衰减     │    ┌─────┼─────┐
    ┌─────────┘    │     │     │
    │         IC恢复│  持续衰减 │ 数据不足待验
    │   ┌──────────┘     │     │
    ▼   ▼                 ▼     │
┌──────────┐        ┌─────────┐ │
│monitoring│───────→│ retired  │ │
│(overloaded)       └─────────┘ │
│  3条路径挤在一起  无恢复路径 ⚠️ │
└──────────┘                     │
     │ IC恢复稳定N天               │
     └──────────→ active ─────────┘
```

### 1.5 `monitoring` 状态的三条进入路径（过载问题）

| 路径 | 来源 | 条件 | 代码位置 |
|------|------|------|---------|
| **P1** | Phase 2 边缘 | `|IC|≥monitoring_min_abs_ic AND ICIR≥monitoring_min_icir` 但不满足full阈值 | `phase2_single.py:137-139` |
| **P2** | active 降级 | 滚动窗口IC偏离>阈值 | `scheduler/attribution.py:189` |
| **P3** | Phase 3 数据不足 | OOS验证因IC历史太短被跳过 | `phase5_monitor.py:339` |

三条路径的因子**性质完全不同**——P1是"可能有微弱信号"，P2是"曾经有效现在衰减"，P3是"还没足够数据判断"——但全挤在`monitoring`一个状态里。

---

## 二、业界标准状态机（综合10端调研）

### 2.1 WorldQuant/Alpha Factory 模式

最完整的因子生命周期管理：

```
proposed ──→ implemented ──→ simulated ──→ validated ──→ production
                │               │              │              │
                └───→ retired   └──→ retired   └──→ retired   ├──→ degraded
                                                               │      │
                                                               │   ┌──┘
                                                               │   │ (恢复)
                                                               │   ▼
                                                               │ validated
                                                               │
                                                               └──→ retired
```

| 状态 | 含义 | 典型停留时间 |
|------|------|------------|
| **proposed** | 假设已提出，尚未编码 | 1-7天 |
| **implemented** | 代码已写，待回测 | 1-3天 |
| **simulated** | IS回测通过 | 1-4周 |
| **validated** | OOS验证通过 | 持续评估 |
| **production** | 实盘运行，有资金分配 | 数月-数年 |
| **degraded** | IC衰减，减仓观察 | 1-4周 |
| **retired** | 永久退役，保留记录 | 永久 |

**关键特征**: ①状态和资金分配分离 ②`degraded`可以先减仓不退役 ③退役后保留记录用于归因

### 2.2 中国平台模式（聚宽/米筐/VNPY统一）

```
注册 ──→ 候选 ──→ 评估中 ──→ 已通过 ──→ 实盘 ──→ 衰减 ──→ 暂停 ──→ 退役
  │                 │                    │         │        │
  └──→ 已拒绝       └──→ 已拒绝           │         └──→ 已恢复 → 实盘
                                          └──→ 退役
```

| 状态 | 含义 |
|------|------|
| **注册(draft)** | 因子定义已入库 |
| **候选(candidate)** | 进入评估流水线 |
| **评估中(evaluating)** | Phase 2/3/4 进行中 |
| **已通过(verified)** | 通过全部评估 |
| **实盘(active/live)** | 参与交易 |
| **衰减(decaying)** | 绩效下滑，观察 |
| **暂停(paused)** | 临时移除，可恢复 |
| **退役(archived)** | 永久移除 |

### 2.3 机构级模式（AQR/Two Sigma/Bloomberg）

```
research ──→ paper_portfolio ──→ small_live ──→ full_live ──→ wind_down ──→ closed
   │              │                  │               │              │
   └──→ closed    └──→ closed        └──→ closed     └──→ closed    └──→ closed
```

机构模式的核心不同：**状态=资金分配比例**。
- `paper_portfolio`: 0% capital，纯跟踪
- `small_live`: 5-10% capital，小资金验证
- `full_live`: 100% target capital
- `wind_down`: 减仓至0%，有序退出

---

## 三、差距分析

| # | 问题 | 当前 | 业界标准 | 严重度 |
|---|------|------|---------|--------|
| **G1** | 缺少初始草稿状态 | 新因子直接`registered`或`inactive`(无效!) | `proposed/draft/research` | 🔴 |
| **G2** | `inactive`不是有效状态 | 脚本中INSERT用`inactive`，但VALID_STATUSES里没有 | 所有状态都经过验证 | 🔴 |
| **G3** | `backtesting`是虚拟标签 | 在VALID_STATUSES但无因子实际持有此状态 | 应该有真实的`evaluating`状态 | 🟡 |
| **G4** | `monitoring`三义过载 | 边缘IC+衰减+数据不足挤在一个状态 | 分离:`marginal`/`degraded`/`insufficient_data` | 🔴 |
| **G5** | `rejected`无恢复路径 | 一旦拒绝永久拒绝 | 市场环境变化后可重新评估 | 🟡 |
| **G6** | `retired`无恢复路径 | 一旦退役永久退役 | 新数据/新方法后可能复活 | 🟡 |
| **G7** | 缺`paused`状态 | 没有办法"暂时移除"一个因子 | `paused/suspended` | 🟡 |
| **G8** | 缺资金分配维度 | 状态不关联资金比例 | 机构:`small_live`/`full_live`/`wind_down` | 🟢(¥5000资本暂不需要) |

---

## 四、改进方案

### 4.1 新状态定义（7个→9个）

| 状态 | 新/旧 | 含义 | 对应业界 |
|------|-------|------|---------|
| **`draft`** | 🆕 | 因子已注册到`factor_registry`，等待诊断预筛 | WorldQuant `proposed` |
| **`candidate`** | 🔄 保留 | 通过诊断预筛，进入Phase 2评估 | 不变 |
| **`evaluating`** | 🆕 替换`backtesting` | Phase 2/3/4 评估进行中 | 中国平台 `evaluating` |
| **`qualified`** | 🆕 替换`registered`(评估前) | 通过全部Phase 2+3+4评估 | WorldQuant `validated` |
| **`active`** | 🔄 保留 | 实盘交易中 | 不变 |
| **`degraded`** | 🆕 拆分monitoring P2 | active降级，IC衰减中 | WorldQuant `degraded` |
| **`monitoring`** | 🔄 保留(瘦身) | 仅用于：Phase 2 边缘 IC + 数据不足 | 缩小职责 |
| **`paused`** | 🆕 | 临时移除（拥挤/人工干预），可恢复 | 中国平台 `paused` |
| **`retired`** | 🔄 保留 | 永久退役 | 不变 |
| **`rejected`** | 🔄 保留 | Phase 2/3/4评估未通过 | 不变 |

**删除的状态:**
- ~~`registered`~~ → 拆分为`draft`(初始) 和`qualified`(评估通过)
- ~~`backtesting`~~ → 替换为`evaluating`(真实状态，非虚拟标签)
- ~~`inactive`~~ → 统一为`draft`

### 4.2 新流转图

```
                         ┌──────┐
                         │draft │  ← 新因子注册
                         └──┬───┘
                            │ 诊断预筛
                    ┌───────┼───────┐
                    │ pass  │ fail  │
               ┌────▼───┐  │       │
               │candidate│  │       │
               └────┬───┘  │       │
                    │      │       │
     ┌──────────────┤      │       │
     │ Phase 2+3+4  │      │       │
     │              │      │       │
 ┌───▼────┐  ┌──────▼──┐   │       │
 │qualified│  │monitoring│  │       │
 │(全通过) │  │(边缘/不足)│  │       │
 └───┬────┘  └────┬─────┘  │       │
     │            │         │       │
     │ 人工激活    │ 积累数据│       │
     │            │         │       │
┌────▼────┐  ┌───▼───┐     │       │
│ active  │  │qualified│    │       │
│(实盘)   │  │(恢复)  │    │       │
└───┬────┘  └───────┘     │       │
    │                      │       │
    │ IC衰减               │       │
┌───▼────┐                 │       │
│degraded│ ← 🆕 从active降级 │       │
└───┬────┘                 │       │
    │          ┌───────────┘       │
    │持续衰减   │ 恢复               │
    │(>buffer) │ (稳定N天)          │
┌───▼──┐  ┌───┴───┐           ┌───▼──────┐
│retired│  │active │           │ rejected  │
│(永久) │  │(恢复) │           │(评估失败) │
└──────┘  └───────┘           └───┬──────┘
                                  │
                            人工触发重新评估
                         (市场环境变化, 新数据)
                                  │
                            ┌─────▼─────┐
                            │ candidate  │ (重新进入评估)
                            └───────────┘

额外路径:
  active ──→ paused ──→ active     (临时暂停, 不经过degraded)
  active ──→ paused ──→ retired    (暂停后决定退役)
  monitoring ──→ rejected           (边缘因子积累数据后仍不达标)
```

### 4.3 状态对比表

| 旧状态 | 新状态 | 迁移逻辑 |
|--------|--------|---------|
| `inactive` | `draft` | 脚本INSERT时改用`draft`；存量数据一次性UPDATE |
| `registered` | `draft` | 未经过诊断预筛的→`draft` |
| `registered`(已诊断) | `candidate` | 诊断预筛通过的→`candidate` |
| `backtesting`(虚拟) | `evaluating` | 三合一过滤器→直接查询`evaluating`状态 |
| `candidate` | `candidate` | 不变 |
| `active` | `active` | 不变 |
| `monitoring`(边缘) | `monitoring` | 保持，职责瘦身为"待观察/数据不足" |
| `monitoring`(衰减) | `degraded` | 拆分出来，明确"曾经有效现在衰减" |
| `rejected` | `rejected` | 不变，但增加人工重新评估入口 |

### 4.4 代码改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `quant/data/repos/factor_repo.py` | 更新`VALID_STATUSES` | 新9状态 |
| `quant/factor/compute/_registry.py` | 更新`_resolve_statuses()` | 移除`backtesting`虚拟标签，改用`evaluating` |
| `quant/factor/compute/_registry.py` | `'using'`→`('active',)` 保持不变 | active仍唯一参与实盘 |
| `quant/evaluation/phase2_single.py` | `get_factor_names("backtesting")`→`get_factor_names("evaluating")` | 评估候选池来源 |
| `quant/evaluation/phase5_monitor.py` | `sync_factor_status()` | 通过→`qualified`(非`active`)，拒绝→`rejected`，边缘→`monitoring` |
| `quant/scheduler/attribution.py` | IC衰减检测 | `active`→`degraded`(非`monitoring`)；`degraded`→`active`(恢复)；`degraded`→`retired`(持续) |
| `scripts/build_fin_factors.sh` | `'inactive'`→`'draft'` | 新因子初始状态 |
| `scripts/activate_candidates.py` | 人工激活 | `qualified`→`active` |
| `docs/migrations/` | 新建迁移SQL | 存量数据状态映射 |

### 4.5 数据迁移

```sql
-- 存量数据状态迁移
UPDATE factor_registry SET status = 'draft'      WHERE status IN ('registered', 'inactive');
UPDATE factor_registry SET status = 'evaluating' WHERE status = 'backtesting';
UPDATE factor_registry SET status = 'degraded'   WHERE status = 'monitoring' 
  AND status_reason LIKE '%degraded%' OR status_reason LIKE '%衰减%';
-- monitoring 的其他条目保留 (边缘IC / 数据不足)
```

### 4.6 新增脚本：人工操作入口

```
scripts/factor_promote.py     # qualified → active (人工确认后激活)
scripts/factor_pause.py       # active → paused (临时暂停)
scripts/factor_resume.py      # paused → active (恢复)
scripts/factor_reeval.py      # rejected/retired → candidate (重新评估)
```

---

## 五、为什么这样改

| 业界标准 | 这个方案如何对齐 |
|---------|----------------|
| WorldQuant: 因子有明确生命周期 | `draft→candidate→evaluating→qualified→active→degraded→retired` |
| 中国平台: 具备暂停和恢复能力 | 新增`paused` + `factor_resume.py` |
| 量化分析师需要知道"为什么这个因子不在用" | `status_reason`字段已有；`degraded`/`rejected`/`retired`语义清晰 |
| 市场环境变化时旧因子可能复活 | `factor_reeval.py`: `rejected`/`retired`→`candidate`重新评估 |
| 评估和实盘之间应有缓冲 | `qualified`≠`active`——通过评估≠自动上线，需人工确认 |
| 临时移除≠永久退役 | `paused` vs `retired` 语义区分 |

---

## 六、暂不改的部分（留待将来）

| 项目 | 原因 |
|------|------|
| **资金分配维度**（paper_live/small_live/full_live/wind_down） | 当前¥5,000起步，资本太小，暂不需要多档资金分配。等资本过了¥100,000再引入。 |
| **因子版本号** | 因子公式修改时产生新版本。当前因子函数通过git管理，暂不需要DB版本号。 |
| **自动重新评估** | 需要一个"市场环境变化检测器"（regime change → trigger re-eval），当前HMM RegimeDetector尚不成熟。 |
