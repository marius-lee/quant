"""同步全市场复权因子 — tushare adj_factor 表。

跑完后 tushare 数据源可对全部股票做 QFQ 复权，不再降级到 zzshare。
每批 50 只，tushare 免费档 200 次/分钟，~110 批约 3 分钟。
"""
from quant.data.store import DataStore
import time

store = DataStore()
t0 = time.time()

for i in range(120):
    r = store.sync_adj_factor(max_batches=1, batch_size=50)
    covered = r.get('covered', 0)
    total = r.get('total', 0)
    new = r.get('new', 0)
    print(f'batch {i+1:3d}: covered={covered}/{total}, new={new}', end='')
    if covered >= total:
        print(' ✅ 完成')
        break
    print()
    time.sleep(0.5)  # 避免触发限流

elapsed = time.time() - t0
store.close()
print(f'\n总耗时: {elapsed:.1f}s')
