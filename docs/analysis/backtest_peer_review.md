# 回测逻辑专业分析 — 同业评审

日期: 2026-07-16

## 一、整体架构

项目有两套回测引擎：
- `quant/backtest/loop.py` — 手工事件驱动循环（主力）
- `quant/backtest/bt_engine.py` — 基于 backtrader.Cerebro 的替代引擎

## 二、与专业量化软件一致的地方 ✅

| 维度 | 行业标准 | 本项目实现 |
|------|---------|-----------|
| T+1 执行 | 信号日与执行日分离 | T日收盘→T+1开盘执行 |
| A股费率 | 万三佣金+千一印花税+滑点 | CostModel: 0.03%+0.1%卖出+0.1%滑点 |
| 整手约束 | 100股整数倍 | LOT_SIZE=100 |
| 涨跌停/停牌 | 无法成交时跳过 | volume>0 过滤 |
| 市场冲击 | Almgren-Chriss sqrt模型 | impact.py |
| 止损 | 8%硬止损+冷却期 | stop_loss_pct=0.08 + 5日冷却期 |
| 交易日历 | 排除周末+节假日 | 本地缓存+akshare |
| WF-IC重训 | 每60天滚动重训IC权重 | retrain_freq=60 |
| 组合优化分级 | 资金量自适应 | Nano等权→Micro倾斜→Small Kelly |

## 三、P0 问题 🔴

### P0-1: 生存偏差 + 前视偏差
- **位置**: `universe_repo.py:25-32`
- **根因**: `stocks` 表是当前全量，包含回测期未上市股票，不含已退市股票
- **影响**: Sharpe高估0.1-0.3（取决于策略偏好）
- **修复方向**: `get_symbols()` 加日期参数，`WHERE s.list_date <= ?`

### P0-2: bt_engine.py 权益曲线Bug
- **位置**: `bt_engine.py:290-296`
- **根因**: `cerebro.broker.getvalue()` 在 `cerebro.run()` 之后始终返回终值
- **影响**: Sharpe/MDD/CAGR 全部算错
- **修复方向**: 用 backtrader analyzer (TimeReturn) 或直接删除 bt_engine.py（未被调用）

## 四、P1 问题 🟡

### P1-1: 缺少基准对比
- Alpha / Information Ratio / Beta / Tracking Error 均未在报告中输出
- pipeline.py 已加载 `benchmark_ret` 但回测报告未使用

### P1-2: 未复权价格的边缘情况
- 两层数据源（akshare qfq + eastmoney fqt=1）均已前复权 ✅
- 第三层 fallback (pytdx) 未显式设置复权 ⚠️ 仅在第一二层都失败时使用

### P1-3: 无 Sortino/Calmar
- 只有 Sharpe。A股高波动特征下 Sortino/Calmar 更实用

## 五、P2 问题

### P2-1: 双引擎分歧
- loop.py 和 bt_engine.py 执行路径完全不同
- bt_engine.py 有已知 Bug、未被实际调用
- 建议: 删除 bt_engine.py，只保留 loop.py

### P2-2: 回测报告增强
- PBO (Probabilistic Sharpe) 代码存在但未接入回测输出
