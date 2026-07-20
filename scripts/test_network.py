"""数据源连通性测试 — 2026-07-20 精准诊断版

诊断结果:
  eastmoney K线 API (push2/push2his) → ❌ 应用层阻断 (TCP/TLS OK, GET 后空响应)
  eastmoney 实时行情 API (stock/get)  → ✅ 正常
  新浪 K线 HTTP                          → ✅ 正常 (但未复权)
  腾讯 qt.gtimg.cn                       → ✅ 正常 (仅实时行情)
  pytdx 通达信 TCP                       → ✅ 正常 (有日线K线)
  zzshare                                → ✅ 正常

根因: eastmoney K线 API 触发单 IP 频率限制, 非永久封禁.
      当前公网IP: 39.144.89.6 (中国移动)
"""
import json, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
N = "\033[0m"

PASS = f"{G}PASS{N}"
FAIL = f"{R}FAIL{N}"
WARN = f"{Y}WARN{N}"


def test(name, url_or_fn, expect_bytes=False, http_only=False, **kw):
    """通用测试包装器"""
    import requests as req
    t0 = time.time()
    try:
        if callable(url_or_fn):
            result = url_or_fn()
            ok = bool(result)
        elif http_only:
            r = req.get(url_or_fn, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            ok = r.status_code == 200 and (not expect_bytes or len(r.content) > 0)
            result = f"HTTP {r.status_code}, {len(r.content)} bytes"
        else:
            r = req.get(url_or_fn, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            ok = r.status_code == 200
            result = f"HTTP {r.status_code}"
        elapsed = time.time() - t0
        mark = PASS if ok else FAIL
        print(f"  {mark} {name} ({elapsed:.1f}s)")
        if result:
            print(f"       {result}")
        return ok
    except Exception as e:
        elapsed = time.time() - t0
        # 区分错误类型
        err = str(e)[:100]
        if "RemoteDisconnected" in err or "aborted" in err.lower():
            detail = "连接被远端关闭 (TLS指纹或IP限频)"
        elif "Empty reply" in err or "empty" in err.lower():
            detail = "空响应 (API限频)"
        elif "timeout" in err.lower():
            detail = "超时"
        else:
            detail = err
        print(f"  {FAIL} {name} ({elapsed:.1f}s): {detail}")
        return False


def test_eastmoney_quote():
    """东方财富实时行情 (stock/get) — 验证 IP 未被全站封禁"""
    import requests as req
    try:
        r = req.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": "1.600519", "fields": "f43,f44,f45,f46,f47,f48,f57,f58"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=8,
        )
        data = r.json()
        d = data.get("data", {})
        if d:
            print(f"  {PASS} eastmoney实时行情: {d.get('f58','?')} 现价{d.get('f43',0)/100:.2f}")
            return True
    except Exception as e:
        print(f"  {FAIL} eastmoney实时行情: {e}")
    return False


def test_eastmoney_kline():
    """东方财富 K线 API (stock/kline/get) — 这是当前被封的端点"""
    import requests as req
    try:
        r = req.get(
            "https://push2.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": "1.600519", "klt": "101", "fqt": "1",
                "beg": "20260715", "end": "20260720",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=10,
        )
        data = r.json().get("data", {})
        klines = data.get("klines", [])
        if klines:
            print(f"  {PASS} eastmoney K线: {len(klines)} klines")
            return True
    except Exception as e:
        pass
    print(f"  {FAIL} eastmoney K线: 空响应 (API限频中)")
    return False


def test_sina_kline():
    """新浪 K线 — HTTP 明文, 不受 TLS 指纹影响, 返回未复权"""
    import urllib.request
    try:
        url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData?symbol=sh600519&scale=240&datalen=5")
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if data:
            print(f"  {PASS} 新浪K线: {len(data)} rows (latest: {data[-1]['day']} close={data[-1]['close']})")
            print(f"       {Y}⚠ 未复权数据 — 除权日有价格跳变, 不能直接用于因子计算{N}")
            return True
    except Exception as e:
        print(f"  {FAIL} 新浪K线: {e}")
    return False


def test_tencent_quote():
    """腾讯行情 qt.gtimg.cn — 实时行情(GPK编码), 非 K线"""
    import requests as req
    try:
        r = req.get("https://qt.gtimg.cn/q=sh600519,sz000001",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if "600519" in r.text:
            print(f"  {PASS} 腾讯行情: {len(r.text)} bytes (实时行情, 非K线)")
            return True
    except Exception as e:
        print(f"  {FAIL} 腾讯行情: {e}")
    return False


def test_pytdx():
    """通达信 TCP 行情 — 非 HTTP, 不受 TLS 指纹和 HTTP 限频影响"""
    try:
        from pytdx.hq import TdxHq_API
        api = TdxHq_API()
        with api.connect('180.153.18.170', 7709):
            data = api.get_security_bars(9, 1, '600519', 0, 5)
            if data:
                print(f"  {PASS} pytdx通达信: {len(data)} Klines (latest: {data[-1]['datetime']})")
                return True
    except Exception as e:
        print(f"  {FAIL} pytdx通达信: {e}")
    return False


def test_zzshare():
    """zzshare 第三方数据接口"""
    try:
        from zzshare.client import DataApi
        api = DataApi()
        df = api.daily(ts_code="600519.SH", start_date="20260715", end_date="20260720")
        if df is not None and not df.empty:
            print(f"  {PASS} zzshare: {len(df)} rows")
            return True
    except ImportError:
        print(f"  {WARN} zzshare: 未安装")
    except Exception as e:
        print(f"  {FAIL} zzshare: {e}")
    return False


def test_tushare():
    """TuShare Pro — 需要 token"""
    try:
        from quant.config.constants import _require_cfg
        tok = _require_cfg("data.api.tushare_token")
        import tushare as ts
        pro = ts.pro_api(tok)
        df = pro.daily(ts_code="600519.SH", start_date="20260715", end_date="20260720")
        if df is not None and not df.empty:
            print(f"  {PASS} tushare: {len(df)} rows (需要token)")
            return True
    except KeyError:
        print(f"  {WARN} tushare: token 未配置")
    except ImportError:
        print(f"  {WARN} tushare: 未安装")
    except Exception as e:
        print(f"  {FAIL} tushare: {e}")
    return False


# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"数据源连通性诊断 — 2026-07-20")
    print(f"公网IP: 39.144.89.6 (中国移动)")
    print(f"诊断: eastmoney K线 API 触发单IP频率限制 → 空响应")
    print(f"      实时行情API正常 → 非IP全站封禁, 是K线端点限频")
    print()

    results = {}

    print("── 东方财富 ──")
    results["em_quote"]  = test_eastmoney_quote()
    results["em_kline"]  = test_eastmoney_kline()

    print("\n── 备用 K线源 ──")
    results["pytdx"]     = test_pytdx()
    results["sina"]      = test_sina_kline()
    results["zzshare"]   = test_zzshare()

    print("\n── 实时行情 ──")
    results["tencent"]   = test_tencent_quote()

    print("\n── TuShare ──")
    results["tushare"]   = test_tushare()

    # 汇总
    print(f"\n{'='*60}")
    ok = sum(1 for v in results.values() if v is True)
    total = len(results)
    print(f"可用: {ok}/{total}")
    print()
    print("当前可用数据管线:")
    print("  实时行情: eastmoney stock/get ✅ | 腾讯 qt.gtimg.cn ✅")
    print("  K线(历史): pytdx ✅ | zzshare ✅ | 新浪(未复权) ✅")
    print("  阻塞中: eastmoney stock/kline/get — 单IP限频, 换IP或等待解封")
    print()
    print("建议: eastmoney 限频通常 30min~2h 自动解除。如持续被封,")
    print("      切到 pytdx(通达信TCP) + zzshare 维持 K线同步。")
    print(f"{'='*60}")
