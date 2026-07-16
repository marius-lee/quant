with open('/Users/mariusto/project/quant/web/app.py', 'r') as f:
    content = f.read()

# Add import for trade_repo and market_conn if needed, and modify the index function
old_index = '''def index():
    return render_template("index.html", version=VERSION)'''

new_index = '''def index():
    """首页 — 传递 perf 数据供服务端渲染仪表盘."""
    try:
        import sqlite3, os
        TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quant", "data", "trades.db")
        tc = sqlite3.connect(TRADE_DB)
        from quant.data.trade_repo import TradeRepo
        base = TradeRepo().get_initial_capital("quant")
        capital = TradeRepo().get_cash("quant") or base
        position_cost = tc.execute(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy='quant' AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy='quant')",
            ("quant", "quant")).fetchone()[0]
        tc.close()
        # use book cost for initial render (no quotes)
        total_asset = round(capital + position_cost, 2)
        total_pnl = round(total_asset - base, 2)
        perf = {"total_pnl": total_pnl, "total_asset": total_asset, "initial_capital": base}
    except Exception:
        perf = {"total_pnl": 0, "total_asset": 5000, "initial_capital": 5000}
    return render_template("index.html", version=VERSION, perf=perf)'''

content = content.replace(old_index, new_index)
with open('/Users/mariusto/project/quant/web/app.py', 'w') as f:
    f.write(content)
print('index() updated')
