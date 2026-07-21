"""快速测试 eastmoney/akshare 是否恢复可用"""
import time, sys
print("=== eastmoney K线 (requests) ===")
try:
    import requests
    r = requests.get(
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101", "fqt": "1", "secid": "1.600000",
            "beg": "20260701", "end": "20260720"
        },
        timeout=10
    )
    print(f"  HTTP {r.status_code}, rows={len(r.json().get('data',{}).get('klines',[]) or [])}")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n=== akshare 单只测试 (换手率) ===")
try:
    import akshare as ak
    t0 = time.time()
    df = ak.stock_zh_a_hist(symbol="600000", period="daily",
                            start_date="20260701", end_date="20260720", adjust="qfq")
    elapsed = time.time() - t0
    if df is not None and not df.empty:
        print(f"  OK {len(df)} rows, {elapsed:.1f}s")
        print(f"  列: {list(df.columns)}")
        if "换手率" in df.columns:
            print(f"  换手率 sample: {df['换手率'].head(3).tolist()}")
    else:
        print("  Empty")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n=== akshare 连续 5 次测试 (限流检查) ===")
try:
    import akshare as ak
    for i in range(5):
        t0 = time.time()
        df = ak.stock_zh_a_hist(symbol="600000", period="daily",
                                start_date="20260717", end_date="20260720", adjust="qfq")
        elapsed = time.time() - t0
        ok = df is not None and not df.empty
        print(f"  #{i}: {'OK' if ok else 'EMPTY'} {elapsed:.1f}s")
        if not ok:
            break
        time.sleep(1.5)  # 保守间隔
except Exception as e:
    print(f"  FAIL at #{i}: {e}")
