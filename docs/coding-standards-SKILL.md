---
name: coding-standards
description: >
  所有编码任务的规范。覆盖防御性编程、分层架构(Web)/量化架构(数据科学)、
  TDD、安全、性能、API 设计、数据模型、并发控制、可观测性、代码审查共 10 个模板。
  生成或修改任何代码时自动激活，逐项校验合规性。模板 2/2a/3 为条件约束，模板 5 限定作用域，其余为硬约束。
---

# 编码规范 — 10 个模板

以下模板 1, 4, 6-10 是硬性约束；模板 2, 2a, 3 为条件约束（见各模板说明）；模板 5 限定性能敏感模块。
校验顺序：防御 → 架构 → 安全 → 性能 → API → 数据 → 并发 → 可观测 → 测试 → 审查。

---

## 模板 1：防御性编程

1. 所有外部输入（函数参数、API 响应、用户输入）必须验证
2. 使用 Early Return 处理错误，减少嵌套
3. 每个 try 必须有特定异常类型，禁止裸 `except`
4. 空值检查用显式比较（`is None` / `== ""`），禁止隐式布尔转换
5. 数值计算检查除零、溢出、精度丢失
6. 文件/网络操作有超时和重试机制
7. 返回结果用 Optional 模式，禁止返回 None 表示错误
8. 错误处理优先级：参数校验 > 业务规则 > 外部依赖 > 系统异常

## 模板 2：分层架构（Web）

**适用范围**: 仅 Web/服务端/多人协作项目强制四层；单机/量化/探索项目使用模板 2a。

依赖方向：Domain ← Application ← Infrastructure ← Interface

- Domain 层：实体（纯数据，零依赖）、领域服务（业务规则，无 IO）、值对象（不可变）
- Application 层：用例类（编排）、DTO（边界）、接口定义（Repository/Service 抽象）
- Infrastructure 层：数据库实现、外部 API 客户端、框架适配
- Interface 层：Controller/Handler、输入验证 → DTO、错误转换（领域异常 → HTTP 状态码）

禁止：外层直接调用内层具体实现，跳过接口。

## 模板 2a：量化/数据科学架构

**适用范围**: 量化研究、数据分析、单用户科学计算项目。原则：IO 与计算分离，避免 DDD 的实体/Repository/DTO 过度抽象。

架构规则：
1. **因子/计算函数**: 纯函数，接收 DataFrame/Series，返回 Series。禁止内部打开数据库连接或调用外部 API
2. **数据层**: 单一职责——获取、缓存、返回数据。接口：`get_X(symbols, date) -> DataFrame`
3. **编排层**: 加载数据 → 调用计算函数 → 组装结果。不包含业务逻辑
4. **入口层**: CLI 脚本或 Web route，解析参数后委托编排层

禁止模式：
- 因子函数内 `import DataStore` 或 `sqlite3.connect()`
- 为 DataFrame 创建 ORM 包装类
- UseCase 类只有两行转发代码

## 模板 3：TDD（测试驱动）

**约束级别**: 软约束, 场景适用时生效。

**强制 TDD**: 确定性的输入→输出转换 — 纯计算函数 (如 factor/compute.py)、数据管道 (ETL)、序列化/反序列化、数据清洗/验证。

**可后补测试**: 探索性代码 — 回测、因子发现/评估、可视化、交互式分析。这些需要人工判断结果正确性，无法预知"正确答案"。稳定后补测试。

按顺序执行（仅强制 TDD 场景）：
1. 先生成测试用例: 正常路径、边界值、异常输入
2. 实现最小功能代码 (只让测试通过), 禁止过度设计
3. 重构（提取重复、优化命名），不添加新功能

框架：pytest。覆盖：分支 >90%，关键路径 100%。

## 模板 4：安全检查

- 输入：白名单验证，SQL 参数化，路径防穿越，命令禁止 `shell=True`
- 输出：HTML 转义，JSON 正确 Content-Type，错误不暴露内部信息
- 认证：bcrypt/argon2，JWT HS256/RS256 禁止 none 算法
- 数据：日志脱敏，配置禁止硬编码密钥，DB 连接 TLS
- 审计：关键操作记录 who/when/what/result

## 模板 5：性能基线

**作用域**: 性能敏感的模块强制执行 (factor/stats_cache, pipeline, backtest, data 批量同步)；一次性 CLI 调试脚本豁免。

**量化项目具体约束**:
- 时间复杂度：因子计算 O(stocks × dates × windows)，截面相关性 O(n_factors² × dates)，禁止嵌套 for 导致 O(stocks²)
- 数据库：WHERE/JOIN/ORDER BY 字段必须命中索引，大表 (margin_detail, daily) 按 date 分区查询
- 空间：加载全市场数据时峰值内存 ≤ 输入数据的 3 倍；DataFrame 操作避免 `.copy()` 链式调用
- 外部调用：JQData/Tushare API 须有超时（连接 5s，读取 30s），失败重试最多 3 次
- 连接复用：避免每个因子函数独立 `sqlite3.connect()` — 使用模块级共享连接或连接池
- 多源并行 I/O: 独立数据源 (不同 API key/rate-limit 池) 使用 ThreadPoolExecutor 并行拉取
  - 示例: JQData + Sina 同时请求，各自 ThreadPoolExecutor worker
  - 反例: 同一 API (如 Tushare) 多线程并行只会更快触发限流，被拒绝
- 优化优先级：算法 (O 优化) > 缓存 (因子值、IC 快照) > 向量化 (pandas/numpy 批量) > 并行 (无状态步骤)
- benchmark：性能敏感模块须在 docstring 标注基线耗时和退化阈值 (如 `stats_cache: ~1s/factor, regression >2x`)

## 模板 6：API 设计

- URL：名词复数，层级清晰（`/users/{id}/orders`）
- 方法语义：GET（幂等）、POST（创建）、PUT（全量更新）、PATCH（部分更新）、DELETE
- 状态码：200/201/204/400/401/403/404/409/429/500
- 响应：`{ "data": ..., "meta": {...}, "error": null }`
- 分页：游标分页（推荐），偏移分页 max_size=100
- 错误格式：`{ "error": { "code": "...", "message": "...", "field": "...", "details": [...] } }`
- 文档：OpenAPI 3.0，含示例和错误场景

## 模板 7：数据模型

**约束级别**: 硬约束 (量化特例见下)。

量化项目三条特例 (SQLite + 分析工作负载的工程现实):

1. **3NF 豁免分析表**: 因子/筛选/估值等分析型宽表可违反 3NF (JOIN 会严重拖慢计算)。
   交易表 (sim_trades)、配置表 (strategy_config) 仍须范式化。

2. **迁移工具替换**: SQLite 不支持 Alembic 的 DROP COLUMN / ALTER COLUMN TYPE 等核心操作。
   改用 `docs/migrations/NNN-description.sql` 编号 SQL 文件 + 应用层幂等 ALTER。
   所有表必须有 `created_at` 时间戳列。

3. **UUID 仅 API 层**: 内部数据表的主键直接用自增 ID 或自然主键 (symbol, date)。
   UUID 仅在 API 暴露资源 ID 时引入 (如 `/api/orders/{uuid}`)。

**通用约束** (所有表):
- 字段类型: 金额 REAL (SQLite 无 DECIMAL)，枚举 CHECK 约束，TEXT 禁用作布尔/状态
- 索引: 每个表至少有一个复合索引覆盖主查询模式 (symbol+date 是量化的通用模式)
- 时间戳必备: `created_at TEXT DEFAULT (datetime('now'))`，关键表加 `updated_at`
- 每表有主: 每张表必须在某个 Python 模块中有且仅有一个建表语句 (不可散落在一
  次性脚本)。建表由 `ensure_tables(conn)` 模式实现，幂等可重入
- 主键策略: 优先自然主键 (symbol, date) 避免冗余自增列；交易流水用自增 ID
- 关系: N:M 用中间表 + 联合主键
- 禁止: 空表残留 (无数据且无写入计划的表必须删除)、元数据表 (meta key-value) 废弃 → 用文件或配置代替

## 模板 8：并发控制

**约束级别**: 条件约束 — 当前单人单机限定作用域；多人多机时立即全量启用。

**激活条件** (满足任一即全量启用):
1. 部署到多台机器 (>1 物理/虚拟服务器)
2. 多个并发用户 (>1 同时活跃)
3. 引入消息队列 (Redis/RabbitMQ) 或分布式任务调度 (Celery)

**当前模式 (单人单机)**:
- IO 密集型: `ThreadPoolExecutor` 多源并行 (见模板5)
- CPU 密集型: 单进程顺序执行 (因子计算等)
- 状态同步: 内存 Lock + SSE 广播 (state_broker.py)

**全量启用后**:
- CPU 密集型 → 进程池，IO 密集型 → 协程
- 锁：范围最小化，禁止锁内 IO，统一加锁顺序防死锁
- 原子操作：乐观锁（version 字段）或 Redis Lua 脚本
- 幂等：唯一索引或 Redis SETNX
- 队列：Celery + Redis/RabbitMQ，背压控制
- 禁止：全局可变状态
- 测试：pytest-asyncio，模拟 race condition

## 模板 9：可观测性

- 日志：JSON 结构化，含 trace_id/span_id，生产 ERROR 全量，DEBUG 1% 采样
- 指标：QPS、P99 延迟、错误率（Prometheus Counter/Gauge/Histogram）
- 追踪：每个请求生成 trace_id，HTTP Header（X-Trace-Id）传播
- 埋点：DB 查询 >100ms 标记慢查询，外部 API 调用，缓存命中/未命中
- 告警：P99 > 500ms 持续 5min，错误率 > 1% 持续 2min
- 实现：OpenTelemetry SDK

## 模板 10：代码审查清单

生成代码后逐项检查：

- 命名：函数动词+名词，布尔 is_/has_/can_，常量全大写，类驼峰
- 结构：函数 <50 行，参数 <5 个，嵌套 <4 层，无重复代码
- 文档：函数有 Args/Returns/Raises/Example（Google 风格）
- 类型：全部类型提示，禁止 Any，mypy --strict 零错误
- 测试：每个 public 函数 ≥3 用例（正常、边界、异常）
- 审查不通过项必须在代码注释标记 `# REVIEWED: 日期`

---

## 执行规则

**生成或修改任何代码前** (硬约束模板 1, 4, 6-10 强制; 模板 2, 2a, 3 按作用域; 模板 5 限定性能敏感模块):
1. 对照 10 个模板逐项校验
2. 发现不合规 → 修复后再输出
3. 无法确定是否合规 → 在代码注释中标注待确认项

**所有参数必须有来源依据**（文献引用、数据校准、或业界标准），禁止凭空设定阈值/常量。
