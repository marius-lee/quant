"""全数据源频率限制测试 — 7源全覆盖"""
import sys, time
sys.path.insert(0, ".")

SYM = "600519"
DATE_START = "20260718"
DATE_END = "20260720"
BURST = 30  # 快速连发次数

def header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def burst_test(name, fn, n=BURST):
    """快速连发 n 次，看何时被限"""
    fails = []
    for i in range(n):
        t0 = time.monotonic()
        try:
            rows = fn()
            elapsed = time.monotonic() - t0
            if rows is None or (hasattr(rows, '__len__') and len(rows) == 0):
                fails.append((i, elapsed, "EMPTY"))
            else:
                n_rows = len(rows) if hasattr(rows, '__len__') else "?"
        except Exception as e:
            elapsed = time.monotonic() - t0
            fails.append((i, elapsed, str(e)[:60]))
    if fails:
        print(f"  ❌ {len(fails)}/{n} 失败:")
        for i, t, err in fails[:5]:
            print(f"     #{i:2d} {t:.2f}s: {err}")
        if len(fails) > 5:
            print(f"     ... +{len(fails)-5} more")
    else:
        print(f"  ✅ {n}/{n} 全部成功")

# ═══════════════════════════════════════
# 1. tushare
# ═══════════════════════════════════════
header("1. tushare (批量50股, 200次/分钟)")
try:
    from quant.config.constants import _require_cfg
    tok = _require_cfg("data.tushare_token")
    import tushare as ts
    ts.set_token(tok)
    pro = ts.pro_api()
    def _test_ts():
        df = pro.daily(ts_code=f"{SYM}.SH", start_date=DATE_START, end_date=DATE_END)
        return len(df) if df is not None and not df.empty else 0
    burst_test("tushare", _test_ts, 10)
except KeyError:
    print("  ⚠ token 未配置, 跳过")
except ImportError:
    print("  ⚠ 未安装, 跳过")

# ═══════════════════════════════════════
# 2. zzshare
# ═══════════════════════════════════════
header("2. zzshare (逐只, 无内置限流)")
from zzshare.client import DataApi
api_zz = DataApi()
def _test_zz():
    df = api_zz.daily(ts_code=f"{SYM}.SH", start_date=DATE_START, end_date=DATE_END)
    return len(df) if df is not None and not df.empty else 0
burst_test("zzshare", _test_zz)

# 不同间隔测试
print("  间隔测试:")
for delay in [0, 0.05, 0.1, 0.2, 0.5]:
    time.sleep(delay)
    t0 = time.monotonic()
    try:
        df = api_zz.daily(ts_code=f"{SYM}.SH", start_date=DATE_START, end_date=DATE_END)
        e = time.monotonic() - t0
        ok = df is not None and not df.empty
        print(f"    sleep={delay:.2f}s: {e:.2f}s → {'OK' if ok else 'EMPTY'}")
    except Exception as ex:
        print(f"    sleep={delay:.2f}s: {time.monotonic()-t0:.2f}s → {ex}")

# ═══════════════════════════════════════
# 3. tickflow
# ═══════════════════════════════════════
header("3. tickflow (批量, 一次请求多只)")
from tickflow import TickFlow
tf = TickFlow.free()

# 单次批量
t0 = time.monotonic()
dfs = tf.klines.batch(["600519.SH"], period="1d", count=5, as_dataframe=True, show_progress=False)
print(f"  1 只: {time.monotonic()-t0:.2f}s, {len(dfs)} results")

# 多只批量
t0 = time.monotonic()
dfs = tf.klines.batch(
    ["600519.SH", "000001.SZ", "300750.SZ", "000858.SZ", "601398.SH"],
    period="1d", count=5, as_dataframe=True, show_progress=False
)
print(f"  5 只: {time.monotonic()-t0:.2f}s, {len(dfs)} results")

# 大批量
t0 = time.monotonic()
codes = [f"{c}.SH" if c.startswith("6") else f"{c}.SZ" for c in
         ["600519","000001","300750","000858","601398","600036","002415","603259",
          "600276","000333","601318","600900","688981","300124","002594",
          "601012","600030","601899","002475","603288"]]
dfs = tf.klines.batch(codes, period="1d", count=5, as_dataframe=True, show_progress=False)
elapsed = time.monotonic() - t0
n_rows = sum(len(df) for df in dfs.values())
print(f"  20 只: {elapsed:.2f}s, {len(dfs)} stocks, {n_rows} rows")

# 快速连发 10 次批量请求
def _test_tf():
    dfs = tf.klines.batch(["600519.SH","000001.SZ"], period="1d", count=5,
                          as_dataframe=True, show_progress=False)
    return sum(len(df) for df in dfs.values())
burst_test("tickflow(batch)", _test_tf, 10)

# ═══════════════════════════════════════
# 4. eastmoney/tencent (curl_cffi)
# ═══════════════════════════════════════
header("4. eastmoney K线 (curl_cffi + chrome131)")
import curl_cffi.requests as cr
def _test_em():
    session = cr.Session()
    r = session.get(
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101", "fqt": "1", "secid": "1.600519",
            "beg": DATE_START.replace("-",""), "end": DATE_END.replace("-",""),
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=10,
        impersonate="chrome131"
    )
    return len(r.json().get("data", {}).get("klines", []))
burst_test("eastmoney/curl_cffi", _test_em, 10)

# ═══════════════════════════════════════
# 5. eastmoney/requests (对比)
# ═══════════════════════════════════════
header("5. eastmoney K线 (requests — 对比)")
import requests as req
def _test_em_req():
    r = req.get(
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101", "fqt": "1", "secid": "1.600519",
            "beg": DATE_START.replace("-",""), "end": DATE_END.replace("-",""),
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        timeout=10,
    )
    return len(r.json().get("data", {}).get("klines", []))
burst_test("eastmoney/requests", _test_em_req, 10)

# ═══════════════════════════════════════
# 6. akshare (monkey-patch curl_cffi)
# ═══════════════════════════════════════
header("6. akshare (monkey-patch curl_cffi)")
import sys as _sys
_orig_req = _sys.modules.get("requests")
import curl_cffi.requests as _curl_req
_sys.modules["requests"] = _curl_req
try:
    import akshare as ak
    def _test_ak():
        df = ak.stock_zh_a_hist(symbol=SYM, period="daily", start_date=DATE_START,
                                end_date=DATE_END, adjust="qfq")
        return len(df) if df is not None and not df.empty else 0
    burst_test("akshare(curl_cffi)", _test_ak, 5)
finally:
    if _orig_req:
        _sys.modules["requests"] = _orig_req

# ═══════════════════════════════════════
# 7. pytdx
# ═══════════════════════════════════════
header("7. pytdx (TCP, 无HTTP限流)")
from pytdx.hq import TdxHq_API
def _test_tdx():
    api = TdxHq_API()
    try:
        with api.connect("180.153.18.170", 7709):
            data = api.get_security_bars(9, 1, SYM, 0, 3)
            return len(data) if data else 0
    finally:
        api.disconnect()
burst_test("pytdx", _test_tdx, 10)

# ═══════════════════════════════════════
# 8. sina
# ═══════════════════════════════════════
header("8. sina (HTTP 明文, 逐只)")
import urllib.request, json
def _test_sina():
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol=sh{SYM}&scale=240&datalen=3")
    req_ = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
    data = json.loads(urllib.request.urlopen(req_, timeout=10).read().decode("utf-8"))
    return len(data)
burst_test("sina", _test_sina, 10)

# ═══════════════════════════════════════
print(f"\n{'='*60}")
print("  测试完成。检查各源 fail 数量和间隔测试结果。")
print(f"{'='*60}")
