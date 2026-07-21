"""诊断 tushare daily_basic 接口 — 返回字段 + 是否支持批量"""
import tushare as ts
from quant.config.constants import _require_cfg

ts.set_token(_require_cfg("data.tushare_token"))
pro = ts.pro_api(timeout=10)

# 1. 单只测试
df = pro.daily_basic(ts_code='600519.SH', trade_date='20260720',
                     fields='ts_code,trade_date,turnover_rate,turnover_rate_f,vol_ratio,pe,pb')
print("=== 单只600519 ===")
print(f"列: {list(df.columns) if not df.empty else 'EMPTY'}")
if not df.empty:
    print(df.to_string())

# 2. 批量测试 (3只)
print("\n=== 批量3只 ===")
df2 = pro.daily_basic(ts_code='600519.SH,000001.SZ,000002.SZ', trade_date='20260720',
                      fields='ts_code,trade_date,turnover_rate')
print(f"列: {list(df2.columns) if not df2.empty else 'EMPTY'}")
print(f"行: {len(df2)}")
if not df2.empty:
    print(df2.to_string())
