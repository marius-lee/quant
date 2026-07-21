"""诊断所有可能提供 turnover 的外部数据源 — 系统7源之外的备选"""
import urllib.request, json, time

def test(name, url, parser, headers=None, is_json=True):
    """通用测试: 返回 (成功, turnover字段名, 样本值)"""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        req.add_header("User-Agent", "Mozilla/5.0")
        body = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        return parser(body, is_json)
    except Exception as e:
        return (False, str(e)[:80], None)

# ── 1. 东方财富实时行情 (test_network 确认可用) ──
def eastmoney_quotes_parser(body, is_json):
    data = json.loads(body)
    if data.get("data") and data["data"].get("diff"):
        row = data["data"]["diff"][0]
        t_keys = [k for k in row if "turnover" in k.lower() or "换手" in k.lower()]
        return (True, t_keys, {k: row[k] for k in t_keys} if t_keys else "no turnover field")
    return (False, "no data", None)

# ── 2. 腾讯日K线 (qfq前复权) ──
def tencent_kline_parser(body, is_json):
    if not is_json:
        # 腾讯返回格式: 分号分隔的字符串
        lines = body.strip().split("\n")
        if len(lines) > 1:
            fields = lines[0].split(" ")
            # 找 turnover 相关字段
            t_fields = [f for f in fields if "turnover" in f.lower() or "换手" in f.lower()]
            return (True, fields[-10:], "turnover_fields: " + str(t_fields) if t_fields else "no turnover")
        return (False, "parse failed", None)
    return (False, "not json", None)

# ── 3. 网易财经 K线 ──
def netease_parser(body, is_json):
    data = json.loads(body)
    if isinstance(data, list) and len(data) > 0:
        row = data[0]
        if isinstance(row, dict):
            return (True, list(row.keys()), "has_turnover" if "turnover" in str(row.keys()).lower() else "no turnover")
    return (False, "no data", None)

# ── 4. tickflow ext 字段深度检查 ──
def tickflow_ext_check():
    try:
        from tickflow import TickFlow
        from quant.config.constants import _require_cfg
        tf = TickFlow(api_key=_require_cfg("data.tickflow_api_key"))
        q = tf.quotes.get(symbols=['600519.SH'])
        if q and 'ext' in q[0]:
            ext = q[0]['ext']
            t_keys = [k for k in ext if 'turnover' in k.lower()]
            return (True, f"ext keys: {list(ext.keys())}", f"turnover_keys: {t_keys}" if t_keys else "no turnover in ext")
        return (False, "no ext", None)
    except Exception as e:
        return (False, str(e)[:80], None)

# ── 5. 腾讯实时行情 (已确认可用, 检查是否有换手率) ──
def tencent_quotes_parser(body, is_json):
    # qt.gtimg.cn 返回格式: var hq_str_xxx="..."
    if "hq_str" in body:
            fields = body.split('"')[1].split(",") if '"' in body else body.split(",")
            # A股行情字段: 0名称 1今开 2昨收 3现价 4最高 5最低 ... 38换手率
            # 来源: http://blog.sina.com.cn/s/blog_53ee262f0102ymme.html
            if len(fields) >= 39:
                return (True, f"field_count={len(fields)}", f"field[38](换手率)={fields[38] if len(fields)>38 else 'N/A'}")
            return (True, f"field_count={len(fields)}", "too few fields")
    return (False, "parse failed", None)

# ── 6. baostock (如已安装) ──
def baostock_check():
    try:
        import baostock as bs
        lg = bs.login()
        rs = bs.query_history_k_data_plus("sh.600519",
            "date,open,high,low,close,volume,turn",
            start_date='2026-07-17', end_date='2026-07-20',
            frequency="d", adjustflag="2")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if rows:
            return (True, "baostock fields: date,open,high,low,close,volume,turn", f"sample: {rows[-1]}")
        return (False, "no rows", None)
    except ImportError:
        return (False, "baostock not installed", None)
    except Exception as e:
        return (False, str(e)[:80], None)

print("=" * 60)
print("数据源 turnover 诊断 — 2026-07-21")
print("=" * 60)

# 1. 东方财富实时行情
ok, info, sample = test("eastmoney实时行情",
    "http://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,f116,f117,f162,f167,f168,f169,f170,f171",
    eastmoney_quotes_parser)
print(f"\n[1] eastmoney实时行情: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

# 2. 腾讯日K线
df = time.strftime("%Y%m%d")
ok, info, sample = test("腾讯日K线",
    f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param=sh600519,day,2026-06-01,2026-07-20,20,qfq",
    tencent_kline_parser, is_json=False)
print(f"\n[2] 腾讯日K线: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

# 3. 网易财经 K线
ok, info, sample = test("网易K线",
    "http://img1.money.126.net/data/hs/kline/day/history/2026/0600519.json",
    netease_parser)
print(f"\n[3] 网易K线: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

# 4. tickflow ext 深度
ok, info, sample = tickflow_ext_check()
print(f"\n[4] tickflow ext深度: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

# 5. 腾讯实时行情
ok, info, sample = test("腾讯实时行情",
    "http://qt.gtimg.cn/q=sh600519",
    tencent_quotes_parser, is_json=False)
print(f"\n[5] 腾讯实时行情: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

# 6. baostock
ok, info, sample = baostock_check()
print(f"\n[6] baostock: {'✅' if ok else '❌'} {info}")
if sample: print(f"    样本: {sample}")

print("\n" + "=" * 60)
