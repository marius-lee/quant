# 架构审查报告 — 2026-05-29

## 核心问题

### 1. pipeline.py 巨石
- `run()` 方法 200 行，做了 8 件事
- 任何环节改动都需修改此文件

### 2. 数据访问层缺失
- `DataStore._connect()` 私有方法被 5 个文件直接调用
- 原始 SQL 散布在 pipeline、auto_run、fundamental 中
- 换存储方案需改所有文件

### 3. 两套重复代码
- auto_run.py `_add_real_factors` vs pipeline.py `_real_fundamental_factors`
- event_engine.py `_build_result` vs metrics.py `compute_metrics`

### 4. config.yaml 未激活
- 28 个配置项零使用
- token/佣金/滑点/批量大小全部硬编码

### 5. 模块边界泄露
- auto_run 知道 SQLite 表名列名
- web/app.py 导入时创建全量引擎实例

## 改进方案

| 阶段 | 内容 |
|------|------|
| 一 | 激活 config.yaml + 数据访问层 + factor_cache 模块 |
| 二 | 拆分 pipeline → 7 子模块 + 统一因子 + 回测调用 metrics |
| 三 | Repository 接口 + Model 协议 |
