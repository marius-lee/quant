from quant.data.store import DataStore
s = DataStore()
conn = s._connect()
symbols = [r[0] for r in conn.execute(
    "SELECT symbol FROM stocks WHERE market!='BJ'"
).fetchall()]
print(f"pulling {len(symbols)} stocks for 2026-07-21")
n = s.update_daily(symbols=symbols, start='2026-07-21')
print(f'new rows: {n}')
s.close()
