import re

src = open('/Users/mariusto/project/quant/quant/scheduler/order_manager.py').read()

# Change 1: Use ask price when available
old_ask = '            ask = q.get("price", 0) or q.get("open", 0) or 0'
new_ask = '''            # 优先使用卖一价 (include_ask_bid), 回退到最新成交价
            ask = q.get("ask", None) or q.get("price", 0) or q.get("open", 0) or 0'''
src = src.replace(old_ask, new_ask)

# Change 2: Add sealed limit-up detection BEFORE the gap calculation
old_block = '''            if ask <= 0:
                continue

            gap = (ask - po.limit_price) / po.limit_price if po.limit_price > 0 else 0'''

new_block = '''            if ask <= 0:
                continue

            # ── 封死涨停检测 (include_ask_bid 提供 ask_volume) ──
            # 卖一量为0 + 价格触及涨停价 → 无人卖出, 无法成交 → 立即放弃
            # 来源: ADR-033 限价单设计, include_ask_bid 实现在 quant/execution/quote.py
            _av = q.get("ask_volume")
            if _av is not None and _av == 0 and not force_now:
                _pc = q.get("prev_close", 0) or 0
                if _pc > 0 and ask > 0:
                    _pct = (ask / _pc - 1)
                    _is_limit = False
                    if po.symbol.startswith(("68", "30")):
                        _is_limit = _pct >= 0.19
                    elif po.symbol.startswith(("4", "8", "92")):
                        _is_limit = _pct >= 0.29
                    else:
                        _is_limit = _pct >= 0.09
                    if _is_limit:
                        self._cancel(po.id, "sealed_limit_up")
                        actions.append({"symbol": po.symbol, "action": "abandon",
                                        "reason": "封死涨停(ask_volume=0), 无法买入"})
                        _log.info(f"[order_manager] ABANDON {po.symbol}: 封死涨停 "
                                  f"(ask={ask:.2f} ask_vol=0), 放弃买入")
                        continue

            gap = (ask - po.limit_price) / po.limit_price if po.limit_price > 0 else 0'''

if old_block in src:
    src = src.replace(old_block, new_block)
    print("Replaced event block")
else:
    print("ERROR: old block not found")
    # Find what's there
    idx = src.find("ask <= 0")
    if idx >= 0:
        print(f"Found at offset {idx}:")
        print(repr(src[idx:idx+200]))

open('/Users/mariusto/project/quant/quant/scheduler/order_manager.py', 'w').write(src)
import ast
ast.parse(src)
print("order_manager.py: OK")
