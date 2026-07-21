"""测试 tushare turnover_rate 返回"""
from quant.config.constants import _require_cfg
import tushare as ts
tok = _require_cfg('data.tushare_token')
ts.set_token(tok)
pro = ts.pro_api(timeout=30)

# 单只
df = pro.daily(ts_code='600519.SH', start_date='20260713', end_date='20260713',
               fields='ts_code,trade_date,turnover_rate')
print(f'600519.SH: rows={len(df) if df is not None else 0}')
if df is not None and len(df) > 0:
    print(df.to_string())
    print(f'turnover_rate: {df["turnover_rate"].tolist()}')

print()

# 批量 3 只
df2 = pro.daily(ts_code='600519.SH,000001.SZ,000002.SZ', start_date='20260713', end_date='20260713',
                fields='ts_code,trade_date,turnover_rate')
print(f'batch 3: rows={len(df2) if df2 is not None else 0}')
if df2 is not None and len(df2) > 0:
    print(df2.to_string())
