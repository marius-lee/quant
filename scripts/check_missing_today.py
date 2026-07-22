from quant.data.store import DataStore
s = DataStore()
conn = s._connect()
all_sym = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market!='BJ'")}
have = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM daily WHERE date='2026-07-21'")}
missing = all_sym - have
print(f'missing: {len(missing)}')
for m in sorted(missing):
    print(m)
s.close()
