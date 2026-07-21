---
# HANDOFF — 2026-07-21 (test-v174, baostock turnover 回填)

## 当前运行状态
- **turnover 回填**: baostock 重写完成, 待手动跑
- **exclude_zero_turnover_days**: 0 (临时关闭, 待回填完成后恢复5)
- **daily 写入模式**: INSERT ... ON CONFLICT DO UPDATE (turnover 受 CASE WHEN 保护)
- **7 源回退链**: 全部可用
- **因子库**: 上次物化 48 因子入库
- **baostock**: 新增依赖, 免费无需注册, turn值与tushare一致

---
## test-v175 — 回填进度日志改进 + CLAUDE.md 编辑规则强化 (2026-07-21)

### 背景
- config.yaml 多次因 apply_patch 产生缩进错误 (4空格→3空格混排)
- backfill_turnover 首批 50 只处理完才出现第一条进度, 用户以为卡住

### 变更
1. **store.py:852** — 新增即时起始日志: "starting, first progress at 50 stocks (~Xs)"
2. **CLAUDE.md** — 编辑工具新增硬约束:
   - YAML 文件禁止 apply_patch, 必须用 yaml.safe_load/dump
   - VERSION 行禁止 apply_patch, 必须用 re.sub
   - 速查表新增 "重启" 规则: Agent 只给命令文本

### 涉及文件
- quant/data/store.py
- web/app.py (VERSION → test-v175)
- CLAUDE.md

---

## test-v174 — baostock turnover 回填重写 (2026-07-21)

### 背景

test-v173 用 sina 做 backfill_turnover, 但 sina K线接口不含 turnover 字段 (实测确认)。
诊断所有可用数据源后, baostock 是唯一满足条件的:

| 源 | turnover字段 | 限速 | 批量 |
|----|-------------|------|------|
| sina K线 | ❌ 无 | 逐只 0.05s | N/A |
| tickflow 行情(免费) | ❌ 无 | 5只/批 10次/分钟 | 5只 |
| tushare daily_basic | ✅ turnover_rate | 1次/分钟 | 支持但限速无用 |
| **baostock** | ✅ **turn** | **0.3s/只 无硬限** | 逐只 |

baostock turn 值与 tushare daily_basic turnover_rate 完全一致 (600519: 0.8492%)。
来源: scripts/check_turnover_sources.py 实测。

### backfill_turnover 重写

**文件**: quant/data/store.py:764-842

**旧策略**: sina HTTP K线, 逐只 urlopen, 读  字段 → 字段不存在 → 永远0 updated
**新策略**: baostock login → 逐只 query_history_k_data_plus(date,turn) → UPDATE daily SET turnover

设计要点:
- 一次 login() 全量循环 logout(), 避免反复登录开销
- 每只 0.3s 间隔 (config: rate_limit.baostock_per_stock_sec)
- 3 次重试 + 指数退避 (2s/4s/6s)
- 每 100 只 commit, 每 500 只打进度日志
- 只写 turnover, 不碰 OHLCV (UPDATE daily SET turnover=? WHERE symbol=? AND date=?)
- 覆盖 last_good 当天也包括 (07-10只有6只BJ有turnover, 其余5421只需补)

时间估算: 5400只 × 0.15s查询 + 0.3s间隔 ≈ 30分钟

### .gitignore 修复

**文件**: .gitignore
 →  +  + 
之前  匹配所有名为 data 的目录, 导致  源码也被 gitignore 屏蔽。

### 诊断脚本

新增 3 个 turnover 数据源诊断脚本:
-  — sina K线返回字段
-  — tickflow 行情 turnover 字段
-  — tushare daily_basic 批量支持
-  — 全数据源对比

---

## 下一步

1. 跑 baostock backfill_turnover:
   [07-21 10:43:06] CRITICAL quant | 未捕获异常: ParserError: while parsing a block mapping
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 53, column 3
expected <block end>, but found '<block mapping start>'
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 74, column 4
  File "<string>", line 2, in <module>
    from quant.data.store import DataStore
  File "/Users/mariusto/project/quant/quant/data/store.py", line 18, in <module>
    from quant.config.loader import load as _load_config
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 199, in <module>
    validate()
    ~~~~~~~~^^
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 65, in validate
    cfg = load()
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 58, in load
    _config = yaml.safe_load(f)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 125, in safe_load
    return load(stream, SafeLoader)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 81, in load
    return loader.get_single_data()
           ~~~~~~~~~~~~~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/constructor.py", line 49, in get_single_data
    node = self.get_single_node()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 36, in get_single_node
    document = self.compose_document()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 55, in compose_document
    node = self.compose_node(None, None)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 133, in compose_mapping_node
    item_value = self.compose_node(node, item_key)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 127, in compose_mapping_node
    while not self.check_event(MappingEndEvent):
              ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 98, in check_event
    self.current_event = self.state()
                         ~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 438, in parse_block_mapping_key
    raise ParserError("while parsing a block mapping", self.marks[-1],
            "expected <block end>, but found %r" % token.id, token.start_mark)

2. 验证 turnover:
   [07-21 10:43:06] CRITICAL quant | 未捕获异常: ParserError: while parsing a block mapping
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 53, column 3
expected <block end>, but found '<block mapping start>'
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 74, column 4
  File "/Users/mariusto/project/quant/scripts/check_turnover_progress.py", line 2, in <module>
    from quant.data.store import DataStore
  File "/Users/mariusto/project/quant/quant/data/store.py", line 18, in <module>
    from quant.config.loader import load as _load_config
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 199, in <module>
    validate()
    ~~~~~~~~^^
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 65, in validate
    cfg = load()
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 58, in load
    _config = yaml.safe_load(f)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 125, in safe_load
    return load(stream, SafeLoader)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 81, in load
    return loader.get_single_data()
           ~~~~~~~~~~~~~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/constructor.py", line 49, in get_single_data
    node = self.get_single_node()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 36, in get_single_node
    document = self.compose_document()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 55, in compose_document
    node = self.compose_node(None, None)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 133, in compose_mapping_node
    item_value = self.compose_node(node, item_key)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 127, in compose_mapping_node
    while not self.check_event(MappingEndEvent):
              ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 98, in check_event
    self.current_event = self.state()
                         ~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 438, in parse_block_mapping_key
    raise ParserError("while parsing a block mapping", self.marks[-1],
            "expected <block end>, but found %r" % token.id, token.start_mark)

3. 回填完成后恢复 config: exclude_zero_turnover_days: 0 → 5
