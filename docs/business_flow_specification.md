# 量化系统业务操作流程详细规范

日期: 2026-06-06  
目标: ¥5000 → ¥100万 (200倍, 6个月)

---

## 总览

```
每日流程 (交易日):

08:30  数据刷新 (日线增量 + 实时行情就绪)
09:00  产业链位置扫描 (V5)  → 决定"做不做"
09:10  板块热度排名 (V4)    → 决定"做什么板块"  
09:15  涨停龙头检测 (V3)    → 决定"买哪只"
09:20  仓位计算 (V2)        → 决定"买多少"
09:25  下单执行
09:30~  盘中监控 + 止损 (V3)
14:50  尾盘评估 + 准备次日
```

---

## Step 1: 产业链位置扫描 (V5) — 09:00

### 1.1 做什么

回答一个问题: **当前市场的主线产业链处于什么阶段?**

```
阶段识别逻辑:

概念期: 政策文件刚出, 游资炒小市值铲子股, 龙头不超3板
  → 信号: 涨停集中在"上游设备材料", 小市值(20-50亿), 换手极高(>30%)
  → 操作: 轻仓试错(≤30%), 只做龙头首板/二板, 次日不涨停即走

扩产期: 上游订单兑现, 业绩开始出现, 机构进场
  → 信号: 涨停向上游扩散, 中市值(50-100亿)开始涨停, 北向/融资持续流入
  → 操作: 加仓至60-100%, 持有龙头3-5天, 允许正常回调

制造期: 上游价格见顶, 利润向中游转移
  → 信号: 上游股开始高位震荡, 中游制造股放量突破
  → 操作: 从上游换仓到中游, 仓位60%

应用期: 全产业链产能过剩, 利润向下游品牌集中
  → 信号: 上游股腰斩, 下游应用股开始获得关注
  → 操作: 减仓至30%, 只做下游龙头

退潮期: 主线全面熄火, 涨停家数<30, 高标炸板频现
  → 操作: 空仓
```

### 1.2 输入数据

| 数据 | 来源 |
|------|------|
| 各概念板块涨停家数(最近5日) | 已有 `limit_up_pattern.py` |
| 各概念板块成分股平均市值分布 | `ak.stock_board_concept_cons_em()` |
| 北向资金板块流入(最近5日) | `ak.stock_sector_fund_flow_rank()` |
| 融资余额板块变化 | Tushare `margin` 接口 |
| 涨停股封板时间/封单量 | 已有 `limit_up_pattern.py` |

### 1.3 输出

```python
{
    "AI": {
        "stage": "扩产期",        # 概念期/扩产期/制造期/应用期/退潮期
        "confidence": 0.8,
        "hot_link": "上游-光模块", # 当前最热的产业链环节
        "next_link": "中游-服务器", # 下一站候选
        "action": "加仓"           # 加仓/持有/减仓/空仓
    },
    "半导体": {"stage": "概念期", ...},
    "新能源": {"stage": "退潮期", "action": "避开"},
}
```

---

## Step 2: 板块热度排名 (V4) — 09:10

### 2.1 做什么

在 V5 标记为"可操作"的产业链中, 对每个概念板块做四维评分:

```
板块热度 = 涨停家数×0.30 + 龙头质量×0.25 + 资金净流入×0.25 + 联动效应×0.20
```

### 2.2 算法

```python
def score_theme(board_code, date):
    stocks = ak.stock_board_concept_cons_em(board_code)  # 成分股
    
    # ① 涨停家数: 板块内今日涨停的股票数
    limit_up_count = count_limit_ups(stocks, date)
    limit_up_score = normalize(limit_up_count)  # 0-1
    
    # ② 龙头质量: 最早封板时间+最大封单+最高连板
    leader = find_leader(stocks, date)
    leader_score = 0.4 * time_score(leader) + 0.3 * order_score(leader) + 0.3 * board_score(leader)
    
    # ③ 资金净流入: 板块主力大单净买入
    fund_flow = get_sector_fund_flow(board_code, date)
    fund_score = normalize(fund_flow)
    
    # ④ 联动效应: 龙头封板后30分钟内, 同板块其他股票拉升≥3%的数量
    linkage = count_linkage(stocks, leader, date)
    linkage_score = normalize(linkage)
    
    return limit_up_score*0.30 + leader_score*0.25 + fund_score*0.25 + linkage_score*0.20
```

### 2.3 输出

```python
[
    {"theme": "光模块", "score": 0.87, "limit_ups": 12, "leader": "XX光电"},
    {"theme": "液冷散热", "score": 0.72, "limit_ups": 8, "leader": "XX环境"},
    {"theme": "HBM存储", "score": 0.65, "limit_ups": 6, "leader": "XX科技"},
]
```

---

## Step 3: 涨停龙头检测 (V3) — 09:15

### 3.1 做什么

在 Top3 板块内, 扫描每只股票, 判断是否满足买入条件。

### 3.2 三大模式并行扫描

**模式A: 首板套利 (北京炒家)**
```
条件:
  - 流通市值 20-100亿
  - 封板时间 < 10:30
  - 换手率 5%-15% (排除死亡换手>50%)
  - 封单金额 > 日成交额20% 或 封单>流通市值1%
  - 板块内≥3只涨停(有梯队)
  - 非ST/次新/一字板/尾盘板(>14:50)

评分: 封板时间分(40%) + 封单质量分(30%) + 板块强度分(20%) + 技术形态分(10%)
```

**模式B: 龙头接力 (陈小群)**
```
条件:
  - 连板≥2板
  - 板块跟风≥5只涨停
  - 流通市值 30-80亿
  - 二板成交量≥首板2/3(充分换手)

评分: 连板高度(35%) + 板块带动性(30%) + 换手质量(20%) + 龙虎榜加分(15%)
```

**模式C: 二板接力 (赵老哥)**
```
条件:
  - 首板爆量(量比≥3)
  - 二板高开2-5%
  - 前15分钟放量突破均线
  - 主线题材(非跟风杂毛)

评分: 量比(40%) + 高开幅度(25%) + 板块地位(25%) + 封板概率(10%)
```

### 3.3 输出

```python
[
    {
        "symbol": "300xxx",
        "name": "XX光电", 
        "score": 0.91,
        "mode": "首板",
        "theme": "光模块",
        "price": 25.80,
        "limit_up_price": 26.50,
        "market_cap": 45,      # 亿
        "board_time": "09:45",
        "order_quality": 0.85,
    },
    {
        "symbol": "688xxx",
        "name": "XX科技",
        "score": 0.83,
        "mode": "龙头接力",
        "boards": 3,
        ...
    }
]
```

---

## Step 4: 仓位计算 (V2) — 09:20

### 4.1 情绪自适应仓位

```python
mood = detect_mood()   # 已有 factor/market_mood.py

if mood["stage"] == "冰点":
    action = "空仓"
    capital_pct = 0
    
elif mood["stage"] == "衰退":
    action = "试错"
    capital_pct = 0.30     # 30%仓位, 1只
    
elif mood["stage"] == "复苏":
    action = "加仓"
    capital_pct = 0.60     # 60%仓位, 1-2只
    
elif mood["stage"] == "扩张":
    action = "重仓"
    capital_pct = 1.0      # 100%仓位, 1-2只
    
elif mood["stage"] == "高潮":
    action = "全仓"
    capital_pct = 1.0      # 100%仓位, 1只 (最强)
```

### 4.2 集中分配

```python
available_cash = total_capital * capital_pct
candidates = stock_results[:2]  # top 2

# allocate_concentrated: 信号强全押, 信号近分散
# score² 加权 → 差距被平方放大
# top1 权重 > 60% → 全押一只
allocs = allocate_concentrated(available_cash, candidates)
```

### 4.3 输出

```python
# 高潮期 × 信号强 → 全仓一只
[
    {"symbol": "300xxx", "shares": 200, "cost": 5160, "weight": 100}
]
```

---

## Step 5: 下单执行 — 09:25

```python
# 1. 卖出旧仓 (不在新推荐中的)
for pos in current_positions:
    if pos.symbol not in new_symbols:
        sell(pos)

# 2. 买入新仓
for alloc in allocations:
    if alloc.symbol in current_positions:
        continue  # 已持有, 不重复买
    if is_limit_up_now(alloc.symbol):
        # 涨停价排板
        place_order(alloc.symbol, 'LIMIT', alloc.price_limit, alloc.shares)
    else:
        # 市价买入
        place_order(alloc.symbol, 'MARKET', None, alloc.shares)
```

---

## Step 6: 盘中监控 + 止损 — 09:30~14:50

### 6.1 实时监控

```
已有功能 (web/app.py /api/trading/exit):
  - 实时价格 (新浪财经)
  - ATR移动止损
  - 3秒刷新
```

### 6.2 铁血止损规则

```python
def check_stop_loss(position, current_price, today_open):
    pnl_pct = (current_price / position.cost - 1) * 100
    high_of_day = position.day_high
    
    # 北京炒家: 次日低开>2% → 3分钟内无条件割
    if position.hold_days == 1:
        open_chg = (today_open / position.cost - 1) * 100
        if open_chg < -2:
            return ("立即止损", "低开>2%")
    
    # 赵老哥: 亏损>5% → 无条件清仓
    if pnl_pct < -5:
        return ("无条件清仓", "亏损>5%")
    
    # 陈小群: 破5日线减半, 破10日线清仓
    if current_price < position.ma5:
        return ("减半仓", "破5日线")
    if current_price < position.ma10:
        return ("清仓", "破10日线")
    
    # ATR移动止损: 从日内高点回落>ATR×2
    if high_of_day > 0:
        dd = (current_price - high_of_day) / high_of_day
        if dd < -position.atr * 2 / 100:
            return ("ATR止损", f"回落{dd*100:.1f}%")
    
    # 北京炒家: 炸板 → 翻绿即出
    if position.was_limit_up and current_price < position.limit_up_price * 0.98:
        return ("炸板止损", "涨停被砸")
    
    return (None, None)  # 继续持有
```

---

## Step 7: 尾盘评估 — 14:50

```python
def end_of_day():
    # ① 未能封板的 → 卖出
    for pos in open_positions:
        if not is_limit_up(pos):
            if pos.pnl > 0:
                sell(pos, reason="尾盘未封板+有盈利")
    
    # ② 持有超过最大天数的 → 卖出
    for pos in open_positions:
        if pos.hold_days >= MAX_HOLD_DAYS:
            sell(pos, reason="到期")
    
    # ③ 更新产业链位置数据 (为明天09:00扫描做准备)
    update_chain_positions()
    update_theme_scores()
    
    # ④ 记录交易日志
    log_trades(today_trades)
```

---

## 北极星数学: 这样能不能到100万?

```
假设: 每月找到2次确定性机会 (产业链传导正确预判)
      每次全仓押注, 平均收益20%
      胜率55% (五大游资的经验值)

月收益 = 2 × 20% × 55% - 2 × 5% × 45% = 22% - 4.5% = 17.5%/月

5000 × (1.175)^6 = 5000 × 2.63 = ¥13,150 (6个月)
5000 × (1.175)^12 = 5000 × 6.92 = ¥34,600 (12个月)
5000 × (1.175)^24 = 5000 × 48.0 = ¥239,800 (24个月)

→ 需要更高频: 每月4次机会, 平均25%收益

5000 × (1 + 4×0.25×0.55 - 4×0.05×0.45)^6 
= 5000 × (1.46)^6 = 5000 × 9.7 = ¥48,500 (6个月)
→ 还是不够200倍

真实路径: 不是匀速复利, 是抓住几次大机会
  抓住1次产业链大爆发 (如2024年9月AI行情, 单月+50%)
  抓住1次龙头连板 (3连板≈+33%)
  5000×1.50×1.33×1.30×1.25×1.20×1.15 ≈ 5000×5.3 ≈ ¥26,500
  → 这还只是6次机会。再翻几倍就需要连板+复利叠加
```

**结论: 200倍靠的不是匀速复利, 是抓住产业链传导中的几次爆发性机会(龙头连板/板块集体涨停), 全仓押注, 然后铁血止损保护本金。**
