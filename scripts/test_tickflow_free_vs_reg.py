"""对比 TickFlow.free() vs TickFlow(api_key=注册版) — 字段/速度/限制"""
import sys, time
sys.path.insert(0, ".")

from tickflow import TickFlow

API_KEY = "tk_868557a55bac4d1e859bf5be94087550"
SYMS = ["600519.SH", "000001.SZ", "300750.SZ"]

def test(label, tf, batch=False):
    print(f"\n--- {label} ---")
    try:
        t0 = time.monotonic()
        if batch:
            dfs = tf.klines.batch(SYMS, period="1d", count=5, as_dataframe=True, show_progress=False)
            elapsed = time.monotonic() - t0
            total = sum(len(d) for d in dfs.values())
            print(f"  batch 3只: {elapsed:.2f}s, {total} rows")
        else:
            df = tf.klines.get("600519.SH", period="1d", count=5, as_dataframe=True)
            elapsed = time.monotonic() - t0
            print(f"  single: {elapsed:.2f}s, {len(df)} rows, cols={list(df.columns)}")
            # 检查换手率
            for col in df.columns:
                if 'turn' in col.lower():
                    print(f"  🔑 turnover 列: '{col}' → {df[col].tolist()}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {str(e)[:80]}")

# 1. 匿名免费
test("TickFlow.free()", TickFlow.free())
test("TickFlow.free() batch", TickFlow.free(), batch=True)

# 2. 注册免费 (api_key)
test("TickFlow(api_key=...)", TickFlow(api_key=API_KEY))
test("TickFlow(api_key=...) batch", TickFlow(api_key=API_KEY), batch=True)

# 3. 速度对比 — 快速连发 5 次
print(f"\n--- 连发 5 次对比 ---")
for label, tf in [("free()", TickFlow.free()), ("api_key", TickFlow(api_key=API_KEY))]:
    times = []
    for i in range(5):
        t0 = time.monotonic()
        try:
            tf.klines.get("600519.SH", period="1d", count=5, as_dataframe=True)
            times.append(time.monotonic() - t0)
        except:
            times.append(None)
    valid = [t for t in times if t]
    if valid:
        print(f"  {label}: avg {sum(valid)/len(valid):.2f}s, {len(valid)}/5 OK")
    else:
        print(f"  {label}: all FAILED")
