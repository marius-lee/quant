"""验证腾讯行情API五档盘口字段位置 — 用于 include_ask_bid 实现."""
import urllib.request, re

SYMBOL = "600519"
code_map = {"6": "sh", "0": "sz", "3": "sz", "4": "bj", "8": "bj", "9": "bj"}
prefix = code_map.get(SYMBOL[0], "sh")
url = f"http://qt.gtimg.cn/q={prefix}{SYMBOL}"

req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com"
})

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("gbk")
except Exception as e:
    print(f"FETCH FAILED: {e}")
    exit(1)

m = re.search(r'v_(\w+)="(.+)"', text)
if not m:
    print(f"PARSE FAILED — raw response:")
    print(text[:600])
    exit(1)

fields = m.group(2).split("~")
print(f"symbol={SYMBOL}  fields={len(fields)}")

# 腾讯QQ股票API字段定义 (cloud.tencent.com/developer/article/1634129)
labels = [
    (0, "市场"), (1, "名称"), (2, "代码"), (3, "最新价"), (4, "昨收"),
    (5, "开盘"), (6, "成交量(手)"), (7, "外盘"), (8, "内盘"),
    (9, "买一价"), (10, "买一量(手)"),
    (11, "买二价"), (12, "买二量(手)"),
    (13, "买三价"), (14, "买三量(手)"),
    (15, "买四价"), (16, "买四量(手)"),
    (17, "买五价"), (18, "买五量(手)"),
    (19, "卖一价"), (20, "卖一量(手)"),
    (21, "卖二价"), (22, "卖二量(手)"),
    (23, "卖三价"), (24, "卖三量(手)"),
    (25, "卖四价"), (26, "卖四量(手)"),
    (27, "卖五价"), (28, "卖五量(手)"),
    (33, "最高"), (34, "最低"), (35, "成交额(万)"), (36, "换手率"),
]

for idx, label in labels:
    if idx < len(fields):
        val = fields[idx]
        print(f"  [{idx:2d}] {label:12s} = {val}")
    else:
        print(f"  [{idx:2d}] {label:12s} = **MISSING**")

# 总结
bid1_price = fields[9] if len(fields) > 9 else ""
bid1_vol = fields[10] if len(fields) > 10 else ""
ask1_price = fields[19] if len(fields) > 19 else ""
ask1_vol = fields[20] if len(fields) > 20 else ""

print(f"\n=== 结论 ===")
print(f"买一: 价={bid1_price}  量={bid1_vol}手")
print(f"卖一: 价={ask1_price}  量={ask1_vol}手")
print(f"ask_volume=0 表示封死涨停 → 无法买入 → monitor应放弃此限价单")
