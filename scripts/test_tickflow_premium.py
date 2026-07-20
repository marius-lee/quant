"""验证 tickflow 付费 API key + 字段清单"""
import sys
sys.path.insert(0, ".")

from tickflow import TickFlow

API_KEY = "tk_868557a55bac4d1e859bf5be94087550"

print("=== tickflow 付费版测试 ===")

# 1. 连接测试
tf = TickFlow(api_key=API_KEY)
print(f"API: {tf._client._base_url}")

# 2. K线单只
print("\n--- K线单只 ---")
df = tf.klines.get("600519.SH", period="1d", count=5, as_dataframe=True)
print(f"columns: {list(df.columns)}")
print(f"rows: {len(df)}")
if not df.empty:
    # Check if turnover/turnover_rate exists
    for col in df.columns:
        if 'turn' in col.lower():
            print(f"  🔑 turnover column found: '{col}'")
            print(f"  sample: {df[col].head().tolist()}")
    print(f"  sample row: {df.iloc[-1].to_dict()}")

# 3. K线批量
print("\n--- K线批量 (3只) ---")
dfs = tf.klines.batch(
    ["600519.SH", "000001.SZ", "300750.SZ"],
    period="1d", count=5, as_dataframe=True, show_progress=True
)
for code, d in dfs.items():
    print(f"  {code}: {len(d)} rows")

# 4. 实时行情 (付费版)
print("\n--- 实时行情 ---")
try:
    quotes = tf.quotes.get(symbols=["600519.SH", "000001.SZ"])
    print(f"  type: {type(quotes)}")
    if hasattr(quotes, 'to_dict'):
        print(f"  data: {quotes.to_dict()}")
    else:
        print(f"  data: {quotes}")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n=== 完成 ===")
