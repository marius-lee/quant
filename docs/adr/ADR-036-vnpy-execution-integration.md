# ADR-036: vnpy 执行层集成方案

**日期**: 2026-07-28
**状态**: ✅ 已实施 (Phase 1 — Adapter 模式落地)
**依赖**: ADR-035 (基础设施替换分析)

---

## 决策

将 `LiveExecutionModel` 执行链底层的 `engine.execute()` 从 SQLite 模拟写入替换为 vnpy 真实券商下单,通过 **Adapter 模式** 保持回测/模拟/实盘三路径代码共享。

## 架构

### 目标:三路径共用 ExecutionModel

```
                    ExecutionModel.run()  (共用链)
                    ├── 冷却过滤 (RiskManager)
                    ├── 硬止损 (check_hard_stop)
                    ├── delta 计算 (compute_trades)
                    ├── validate + alpha 裁剪 (trim_orders_by_alpha)
                    └── execute_buys / execute_sells  (子类实现)
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
BacktestExecutionModel   SimExecutionModel    LiveExecutionModel
(模拟,已有)             (模拟,已有默认)      (vnpy,新增)
        │                     │                     │
        ▼                     ▼                     ▼
  engine.execute()      engine.execute()     BrokerAdapter
  (SQLite 写入)         (SQLite 写入)        ├── submit_order()
                                             ├── cancel_order()
                                             ├── get_positions()
                                             ├── get_account()
                                             └── get_orders()

                                                  │
                                    ┌─────────────┼─────────────┐
                                    ▼             ▼             ▼
                             VnpyCtpAdapter  VnpyXtpAdapter  SimulatedAdapter
                             (CTP 期货)      (XTP 股票)      (回退用)
```

### 新增模块

```
execution/
├── broker_adapter.py    ← NEW: BrokerAdapter 抽象基类 + SimulatedAdapter + vnpy适配器注册
├── execution_model.py   ← MODIFY: LiveExecutionModel 接收 adapter
├── engine.py            ← MODIFY: ExecutionEngine 支持 adapter 模式
├── (其他文件不变)
```

## Phase 1 实施记录 (2026-07-28)

### 新增文件
- `quant/execution/broker_adapter.py` — 券商适配器模块 (470行)
  - `BrokerAdapter` — 抽象基类 (buy/sell/cancel/get_positions/get_account/get_orders)
  - `SimulatedAdapter` — 封装现有 ExecutionEngine，行为完全不变（默认）
  - `VnpyAdapter` — vnpy 券商骨架 (自动检测 vnpy 可用性)
  - `VnpyCtpAdapter` / `VnpyXtpAdapter` — CTP/XTP 子类
  - `OrderResult` / `AccountInfo` — 统一返回类型
  - `get_broker_adapter()` — 工厂函数 + 进程级单例

### 修改文件
- `quant/execution/engine.py` — ExecutionEngine 增加 `broker_adapter` 参数
- `quant/execution/execution_model.py` — `execute_sells()` 优先通过 adapter
- `quant/scheduler/execute.py` — 启动时创建 broker adapter
- `quant/scheduler/monitor.py` — `_execute_sell()` 适配 adapter 模式
- `quant/scheduler/order_manager.py` — `_fill()` 通过 adapter 成交 + 同步 sim_trades
- `quant/config/config.yaml` — 新增 `execution.broker` 配置段

### 测试结果
- 全部 185 个现有测试通过 ✅
- 执行相关测试 23 个全过 ✅
- vnpy 不可用时自动回退 SimulatedAdapter（行为不变）

### 未实施 (需 vnpy 环境)
- 安装 vnpy: `pip install vnpy vnpy_ctp vnpy_xtp`
- 配置: `execution.broker.adapter: "vnpy_ctp"` + settings
- 事件回调 → sim_trades 同步（当前 VnpyAdapter._on_order/_on_trade 为空）

---

## 设计原则

1. **Adapter 模式**:执行操作通过抽象接口调用,具体实现可替换
2. **零配置默认**:未安装 vnpy 时自动回退到 SimulatedAdapter(行为不变)
3. **配置驱动**:`config.yaml` 控制使用哪个 adapter
4. **最少修改**:RiskManager、CostModel、calendar、constraints 全部不动

## BrokerAdapter 接口设计

```python
class BrokerAdapter(ABC):
    """券商适配器抽象基类 - 执行层的最小接口。"""

    @abstractmethod
    def connect(self) -> bool:
        """连接券商网关,返回是否成功。"""

    @abstractmethod
    def disconnect(self):
        """断开连接。"""

    @abstractmethod
    def buy(self, symbol: str, price: float, shares: int,
             order_type: str = "LIMIT") -> OrderResult:
        """提交买单。"""

    @abstractmethod
    def sell(self, symbol: str, price: float, shares: int,
              order_type: str = "MARKET") -> OrderResult:
        """提交卖单。"""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """撤单。"""

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """获取当前持仓。"""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户信息(总资产、可用资金)。"""

    @abstractmethod
    def get_orders(self, status: str = None) -> list[dict]:
        """获取订单列表。"""

    @abstractmethod
    def is_connected(self) -> bool:
        """连接状态。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """适配器名称 (ctp / xtp / simulated)。"""
```

## 实现计划

### Phase 1: 基础设施(本次)
- [ ] 创建 `execution/broker_adapter.py`
  - `BrokerAdapter` 抽象基类
  - `SimulatedAdapter` - 封装现有 `ExecutionEngine`,行为完全不变
  - `VnpyAdapter` - vnpy 适配器骨架(含导入检测、连接管理、下单逻辑)
- [ ] 修改 `ExecutionEngine` 支持 adapter 注入
- [ ] 修改 `LiveExecutionModel` 通过 adapter 执行
- [ ] 修改 `OrderManager` 通过 adapter 成交
- [ ] 修改 `scheduler/monitor.py` 通过 adapter 卖单
- [ ] 添加 `config.yaml` 配置项

### Phase 2: vnpy 实际对接(需 vnpy 环境)
- [ ] 安装 vnpy 4.4+
- [ ] 实现 `VnpyCtpAdapter`(CTP 柜台)
- [ ] 实现 `VnpyXtpAdapter`(XTP 柜台)
- [ ] 实现事件回调 → `OrderResult` 映射
- [ ] 集成测试(模拟盘)

### Phase 3: 行情源切换(可选)
- [ ] vnpy 行情 → 替换 quote.py 的腾讯/新浪爬虫
- [ ] 保留 fallback 链路

## 配置

```yaml
# config/config.yaml 新增
execution:
  broker:
    adapter: "simulated"       # simulated | vnpy_ctp | vnpy_xtp
    vnpy:
      gateway: "CtpGateway"    # CtpGateway | XtpGateway
      settings:
        username: ""
        password: ""
        broker_id: ""
        td_address: ""
        md_address: ""
```

## 风险评估

| 风险 | 缓解 |
|---|---|
| vnpy API 版本不兼容 | SimulatedAdapter 始终可用作为回退 |
| 网络断开导致订单丢失 | is_connected() 健康检查 + 重连机制 |
| 成交回报延迟 | 现有 OrderManager 已处理挂单/追价/补单 |
| vnpy 与 SQLite schema 不一致 | Adapter 输出统一为 OrderResult,内部适配 |

## 相关文件

| 文件 | 变更类型 |
|---|---|
| `execution/broker_adapter.py` | NEW |
| `execution/engine.py` | MODIFY |
| `execution/execution_model.py` | MODIFY |
| `scheduler/execute.py` | MODIFY |
| `scheduler/monitor.py` | MODIFY |
| `scheduler/order_manager.py` | MODIFY |
| `config/config.yaml` | MODIFY |
