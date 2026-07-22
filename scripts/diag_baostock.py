"""诊断 baostock 返回数据格式 — 用于 backfill_turnover 调试."""
import baostock as bs

bs.login()
print("login:", bs.login().error_msg if hasattr(bs, 'login') else 'N/A')

# 测试: sh.600519 2026-07-10
codes = ["sh.600519", "sz.000001", "sh.920082"]
dates = ["2026-07-10", "2026-07-20"]

for d in dates:
    print(f"\n=== date={d} ===")
    for code in codes:
        rs = bs.query_history_k_data_plus(
            code, "date,turn",
            start_date=d, end_date=d,
            frequency="d", adjustflag="2")
        if rs.error_code == '0':
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                r = rows[0]
                tv = float(r[1]) if len(r) > 1 and r[1].strip() else 0.0
                print(f"  {code}: row={r}  date_match={r[0]==d}  turn={tv}")
            else:
                print(f"  {code}: 0 rows")
        else:
            print(f"  {code}: error — {rs.error_msg}")

bs.logout()
print("done")
