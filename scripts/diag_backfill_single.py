"""模拟 backfill_turnover 对一只股票的处理流程 (精简版)."""
import time
import baostock as bs

bs.login()
print("baostock login OK")

d = "2026-07-10"
code = "sh.600519"

tv = 0.0
for retry in range(3):
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,turn",
            start_date=d, end_date=d,
            frequency="d", adjustflag="2")
        ec = rs.error_code
        print(f"  retry={retry} error_code='{ec}' error_msg='{rs.error_msg}'")
        if ec == '0':
            while rs.next():
                row = rs.get_row_data()
                print(f"  row={row} len={len(row)}")
                if row[0] == d:
                    tv_str = row[1] if len(row) > 1 else ''
                    print(f"  tv_str={repr(tv_str)}")
                    tv = float(tv_str) if tv_str and tv_str.strip() else 0.0
                    print(f"  tv={tv} tv>0={tv>0}")
                    break
        else:
            print(f"  *** baostock returned error_code='{ec}' — SILENTLY IGNORED ***")
        break
    except Exception as e:
        print(f"  exception: {e!r}")
        if retry < 2:
            time.sleep(2 * (retry + 1))

print(f"\nFINAL: tv={tv} would_update={tv>0}")
bs.logout()
