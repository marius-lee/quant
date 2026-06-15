"""下单前风控检查 + 持仓止损监控 + 资金回撤熔断。

检查项:
  1. 可用资金 ≥ 订单金额
  2. 股票未涨停/跌停
  3. 单票仓位 ≤ 上限
  4. 总持仓数 ≤ 上限
  5. 订单股数 ≥ 100 (A股最小交易单位)
  6. 个票止损线 — 亏损超阈值强制卖出
  7. 日亏损限制 — 当日亏损超限禁止新开仓
  8. 最大回撤熔断 — 总回撤超限强制清仓
"""

from datetime import date, datetime
from typing import Optional

from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("execution.risk")


class RiskChecker:
    """下单前风控检查器 + 持仓监控"""

    def __init__(self, data_store=None, live_store=None):
        self.store = data_store
        self.live = live_store  # live_broker module, optional

    # ── 买入检查 ──

    def check_buy(self, symbol: str, shares: int, price: float,
                  cash: float, positions: dict, prev_close: float = None,
                  daily_pnl: Optional[dict] = None) -> dict:
        """检查买入订单是否合规。

        Args:
            daily_pnl: 当日盈亏信息 {total_asset, daily_return, cumulative_return}
        """
        # 来源: A股交易规则 — 最小交易单位=100股(1手)
        if shares < 100:
            return {"ok": False, "reason": f"股数{shares}<100(最小交易单位)", "cost": 0}

        cost = shares * price
        # 来源: A股规则 — 佣金万3(最低5元) + 印花税千1(卖)
        commission = max(5.0, cost * 0.0003)
        total_cost = cost + commission

        if total_cost > cash:
            return {"ok": False, "reason": f"资金不足(需¥{total_cost:.0f}, 可用¥{cash:.0f})", "cost": total_cost}

        max_positions = cfg("backtest.max_positions", 3)
        if symbol not in positions and len(positions) >= max_positions:
            return {"ok": False, "reason": f"持仓已达上限{max_positions}只", "cost": total_cost}

        # 涨停检查
        if prev_close and prev_close > 0:
            chg = price / prev_close - 1
            if chg > 0.095:
                return {"ok": False, "reason": f"{symbol}涨停(chg={chg*100:.1f}%)", "cost": total_cost}

        # 日亏损限制 — 当日亏超限禁止开新仓
        if daily_pnl:
            daily_loss_limit = cfg("risk.daily_loss_limit", 0.20)
            dr = daily_pnl.get("daily_return", 0)
            if dr < -daily_loss_limit:
                return {"ok": False, "reason": f"日亏损超限({dr*100:.1f}%), 禁止开仓", "cost": total_cost}

        # 最大回撤熔断 — 禁止开仓
        if daily_pnl:
            cum_ret = daily_pnl.get("cumulative_return", 0)
            max_dd_line = -cfg("risk.max_drawdown", 0.80)
            if cum_ret < max_dd_line:
                return {"ok": False, "reason": f"触发回撤熔断(累计{cum_ret*100:.1f}%), 禁止开仓", "cost": total_cost}

        return {"ok": True, "reason": "", "cost": total_cost}

    # ── 卖出检查 ──

    def check_sell(self, symbol: str, shares: int, price: float,
                   positions: dict, prev_close: float = None) -> dict:
        """检查卖出订单是否合规。"""
        if symbol not in positions:
            return {"ok": False, "reason": f"未持有{symbol}", "proceeds": 0}

        pos_data = positions[symbol]
        held_shares = pos_data.get("shares", 0) if isinstance(pos_data, dict) else pos_data
        if held_shares < shares:
            shares = held_shares
        if shares < 100:
            return {"ok": False, "reason": f"可卖股数{shares}<100", "proceeds": 0}

        if prev_close and prev_close > 0:
            chg = price / prev_close - 1
            if chg < -0.095:
                return {"ok": False, "reason": f"{symbol}跌停(chg={chg*100:.1f}%)", "proceeds": 0}

        proceeds = shares * price
        commission = max(5.0, proceeds * 0.0003)
        stamp_tax = proceeds * 0.001
        net_proceeds = proceeds - commission - stamp_tax
        return {"ok": True, "reason": "", "proceeds": net_proceeds}

    # ── 止损监控 ──

    def check_stop_loss(self, positions: list[dict], store=None) -> list[dict]:
        """检查所有持仓是否触发止损。

        Args:
            positions: [{"symbol", "shares", "cost_price", "latest_price", "pnl_pct"}]
                       pnl_pct 已统一为百分比形式 (如 -15.5 表示 -15.5%)
        """
        stop_loss_pct = cfg("risk.stop_loss_pct", -0.15)  # 小数-0.15
        alerts = []

        for p in positions:
            pnl_pct = p.get("pnl_pct", 0)
            # pnl_pct 现在是统一的百分比形式 (get_positions 返回值)
            # 转换为小数: -15.5 → -0.155
            loss = pnl_pct / 100.0

            if loss < stop_loss_pct:
                alerts.append({
                    "symbol": p["symbol"],
                    "action": "sell",
                    "reason": f"止损: 亏损{loss*100:.1f}% > {abs(stop_loss_pct)*100:.0f}%",
                    "loss_pct": round(loss * 100, 2),
                    "current_price": p.get("latest_price", 0),
                    "cost_price": p.get("cost_price", 0),
                    "shares": p.get("shares", 0),
                })
                logger.warning(f"STOP LOSS: {p['symbol']} loss={loss*100:.1f}% cost=¥{p.get('cost_price',0):.2f}")

        return alerts

    # ── 持仓限制检查 ──

    def check_position_limits(self, positions: list[dict], prices: dict = None,
                              cash: float = 0) -> list[dict]:
        """检查持仓是否触发风控线，返回告警列表。

        检查项:
          - 单票跌幅超过止损线
          - 总回撤超过最大回撤
          - 单票占比是否过高
        """
        alerts = []

        # 止损检查
        stop_alerts = self.check_stop_loss(positions)
        alerts.extend(stop_alerts)

        # 单票集中度检查
        total_value = sum(
            p.get("current_value", p.get("shares", 0) * p.get("cost_price", 0))
            for p in positions
        )
        for p in positions:
            value = p.get("current_value", p.get("shares", 0) * p.get("cost_price", 0))
            if total_value > 0 and value / total_value > 0.60:
                alerts.append({
                    "type": "concentration",
                    "symbol": p.get("symbol", ""),
                    "action": "reduce",
                    "reason": f"单票集中度{value/total_value*100:.0f}% > 60%",
                    "weight": round(value / total_value * 100, 1),
                })

        return alerts

    # ── 全量风控扫描 ──

    def full_scan(self, positions: list[dict], cash: float, store=None) -> dict:
        """全量风控扫描，返回可执行动作列表。

        Returns:
            {
                "status": "ok" | "warning" | "critical",
                "actions": [{"symbol", "action", "reason", "urgency"}],
                "alerts": [str],
                "block_new_positions": bool,
            }
        """
        actions = []
        alert_msgs = []
        block_new = False

        # 1. 止损扫描
        stop_alerts = self.check_stop_loss(positions, store)
        for sa in stop_alerts:
            actions.append({**sa, "urgency": "high"})
            alert_msgs.append(f"🔴 止损 {sa['symbol']}: {sa['reason']}")

        # 2. 集中度检查
        pos_alerts = self.check_position_limits(positions, cash=cash)
        for pa in pos_alerts:
            if pa.get("action") not in ("sell",):
                actions.append({**pa, "urgency": "medium"})
                alert_msgs.append(f"🟡 {pa.get('symbol', '')} {pa.get('reason', '')}")

        # 2.5. 连续亏损检测 — 陈小群仓位风控
        try:
            from execution.live_broker import get_trade_history
            trades = get_trade_history(limit=20)
            consecutive_losses = 0
            for t in trades:
                if t["side"] == "sell":
                    # 粗略估算: 卖出金额 vs 买入成本
                    cost = sum(b["amount"] for b in trades if b["side"] == "buy" and b["symbol"] == t["symbol"])
                    if cost > 0 and t["amount"] < cost:
                        consecutive_losses += 1
                    else:
                        break
                else:
                    break
            if consecutive_losses >= 2:
                actions.append({
                    "action": "reduce_position",
                    "reason": f"连续亏损{consecutive_losses}笔, 仓位减半",
                    "urgency": "high",
                })
                block_new = True
                alert_msgs.append(f"🟡 连续亏损{consecutive_losses}笔, 建议减仓")
        except Exception:
            pass

        # 3. 回撤检查
        from execution.live_broker import get_pnl_summary
        try:
            summary = get_pnl_summary()
            cum_ret = summary.get("total_return_pct", 0) / 100
            max_dd = -cfg("risk.max_drawdown", 0.80)
            if cum_ret < max_dd:
                block_new = True
                alert_msgs.append(f"🔴 最大回撤熔断: 累计{cum_ret*100:.1f}% < {max_dd*100:.0f}%")
                if cum_ret < max_dd * 1.5:
                    actions.append({
                        "action": "liquidate_all",
                        "reason": f"回撤熔断: 累计{cum_ret*100:.1f}%",
                        "urgency": "critical",
                    })
        except Exception:
            pass

        # 判定状态
        if any(a.get("urgency") == "critical" for a in actions):
            status = "critical"
        elif actions:
            status = "warning"
        else:
            status = "ok"

        return {
            "status": status,
            "actions": actions,
            "alerts": alert_msgs,
            "block_new_positions": block_new,
        }
