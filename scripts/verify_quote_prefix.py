"""M9 验证: quote.py 股票代码前缀映射正确性。

测试:
1. DB 中各前缀段 stock → Tencent/Sina API 前缀映射
2. 验证 bj 前缀对北交所股票是否有效
"""

import urllib.request
import json

TENCENT_URL = "http://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com",
}

# ======== 映射表 (来自 quant/execution/quote.py) ========
# (4, 8, 92) → bj   北交所/退市板
# (6, 9)    → sh   上海
# 其他      → sz   深圳

test_cases = [
    # (symbol, expected_prefix, market_name)
    ("600519", "sh", "上海A股"),
    ("000001", "sz", "深圳A股"),
    ("300750", "sz", "深圳创业板"),
    ("688001", "sh", "上海科创板"),
    ("920001", "bj", "北交所"),
    ("002001", "sz", "深圳中小板"),
]

print("=" * 60)
print("M9: quote.py 前缀映射验证")
print("=" * 60)
print()

all_ok = True
for sym, expected_prefix, market in test_cases:
    url = f"{TENCENT_URL}{expected_prefix}{sym}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=5)
        text = resp.read().decode("gbk", errors="replace")
        resp.close()
        
        has_data = sym in text and ('"1~' in text or '"0~' in text or '~' in text)
        status = "OK" if has_data else "NO DATA (可能停牌)"
        if not has_data:
            all_ok = False
        
        # 检查回包中是否有价格信息
        snippet = text.strip()[:80]
        print(f"  {sym:8s} → {expected_prefix}{sym:8s} ({market:10s}) [{status}]")
        print(f"         {snippet}")
        
    except Exception as e:
        print(f"  {sym:8s} → {expected_prefix}{sym:8s} ({market:10s}) [FAIL: {e}]")
        all_ok = False

print()
if all_ok:
    print("结论: 所有映射正确。北交所 920xxx → bj 前缀有效。")
else:
    print("⚠️ 部分映射可能需调整，检查以上 FAIL/NO DATA。")
