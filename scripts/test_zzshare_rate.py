"""测试 zzshare + tickflow 频率限制"""
import sys, time
sys.path.insert(0, ".")

print("=" * 50)
print("zzshare 频率限制测试")
print("=" * 50)

from zzshare.client import DataApi
api = DataApi()

# 快速连发 20 次，看何时被限
SYM = "600519"
results = []
for i in range(20):
    t0 = time.monotonic()
    try:
        df = api.daily(ts_code=f"{SYM}.SH", start_date="20260718", end_date="20260720")
        elapsed = time.monotonic() - t0
        ok = df is not None and not df.empty
        results.append((i, f"{elapsed:.2f}s", "OK" if ok else "EMPTY"))
    except Exception as e:
        elapsed = time.monotonic() - t0
        results.append((i, f"{elapsed:.2f}s", str(e)[:50]))

print(f"{'#':>3} {'time':>6} {'result'}")
for r in results:
    print(f"{r[0]:3d} {r[1]:>6} {r[2]}")

# 慢速测试：不同间隔
print("\n" + "=" * 50)
print("zzshare 间隔测试 (不同 sleep)")
print("=" * 50)
for delay in [0, 0.1, 0.5, 1.0]:
    t0 = time.monotonic()
    try:
        df = api.daily(ts_code=f"{SYM}.SH", start_date="20260718", end_date="20260720")
        elapsed = time.monotonic() - t0
        ok = df is not None and not df.empty
        print(f"  delay={delay:.1f}s: {elapsed:.2f}s → {'OK' if ok else 'EMPTY'}")
    except Exception as e:
        print(f"  delay={delay:.1f}s: {time.monotonic()-t0:.2f}s → {e}")
    time.sleep(delay)

print("\n" + "=" * 50)
print("tickflow 批量接口测试")
print("=" * 50)
try:
    from tickflow import TickFlow
    tf = TickFlow.free()
    t0 = time.monotonic()
    dfs = tf.klines.batch(
        ["600519.SH", "000001.SZ", "300750.SZ"],
        period="1d", count=5,
        as_dataframe=True, show_progress=False
    )
    elapsed = time.monotonic() - t0
    print(f"  3 stocks batch: {elapsed:.2f}s, {len(dfs)} results")
    for code, df in dfs.items():
        print(f"    {code}: {len(df)} rows")
except Exception as e:
    print(f"  tickflow: {e}")
