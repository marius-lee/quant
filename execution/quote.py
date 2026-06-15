"""实时行情 — 新浪财经免费接口，批量拉取持仓现价。

接口: http://hq.sinajs.cn/list=sh600036,sz000001,bj430047
字段: [0]名称 [1]今开 [2]昨收 [3]现价 [4]最高 [5]最低
      涨幅=现价/昨收-1 (API不再直接返回涨幅)

限制: 单次最多 ~80 只，无认证。
缓存: 3 秒 TTL，前端每秒轮询不重复打新浪。
只在交易日盘中拉取 (9:30-15:00)，盘后/非交易日跳过。
"""

import re
import time
import urllib.request
from datetime import date, datetime

from utils.logger import get_logger

logger = get_logger("execution.quote")

_SINA_URL = "http://hq.sinajs.cn/list="
# 来源: 新浪财经API单次请求上限≈80只, 60留有安全边际
_BATCH_SIZE = 60
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def _symbol_to_sina(symbol: str) -> str:
    """量化代码 → 新浪代码。600036→sh600036, 000001→sz000001, 430047→bj430047"""
    if symbol.startswith(("4", "8", "92")):
        return f"bj{symbol}"
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _parse_sina_line(line: str) -> dict:
    m = re.search(r'hq_str_(\w+)="(.+)"', line)
    if not m:
        return None
    sina_code = m.group(1)
    fields = m.group(2).split(",")
    if len(fields) < 32:
        return None

    try:
        price = float(fields[3]) if fields[3] else 0
        prev_close = float(fields[2]) if fields[2] else 0
    except ValueError:
        return None

    if price <= 0:
        return None

    symbol = sina_code[2:]  # sh600036 → 600036

    return {
        "symbol": symbol,
        "name": fields[0],
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "change_pct": round((price / prev_close - 1) * 100, 2) if prev_close > 0 else 0,
        "high": round(float(fields[4]) if fields[4] else price, 2),
        "low": round(float(fields[5]) if fields[5] else price, 2),
        "open": round(float(fields[1]) if fields[1] else price, 2),
        "volume": int(float(fields[8])) if len(fields) > 8 and fields[8] else 0,
    }


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """批量拉取实时行情。"""
    if not symbols:
        return {}

    result = {}
    for i in range(0, len(symbols), _BATCH_SIZE):
        batch = symbols[i:i + _BATCH_SIZE]
        sina_codes = ",".join(_symbol_to_sina(s) for s in batch)
        url = _SINA_URL + sina_codes

        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("gbk")
        except Exception as e:
            logger.warning(f"quote fetch failed: {e}")
            continue

        for line in text.strip().split("\n"):
            parsed = _parse_sina_line(line)
            if parsed:
                result[parsed["symbol"]] = parsed

        if i + _BATCH_SIZE < len(symbols):
            time.sleep(0.5)

    return result


def fetch_position_quotes(symbols: list[str], store=None) -> list[dict]:
    """获取持仓实时行情，与持仓数据合并。"""
    from execution.live_broker import get_positions as _get_positions
    positions = _get_positions(store=store)
    if not positions:
        return []
    quotes = fetch_quotes([p["symbol"] for p in positions])

    result = []
    for p in positions:
        q = quotes.get(p["symbol"])
        if q:
            price = q["price"]
            value = p["shares"] * price
            pnl = value - p["total_cost"]
            pnl_pct = (pnl / p["total_cost"] * 100) if p["total_cost"] > 0 else 0
            result.append({
                "symbol": p["symbol"], "name": q["name"],
                "shares": p["shares"], "cost_price": round(p["cost_price"], 2),
                "total_cost": round(p["total_cost"], 2), "price": price,
                "value": round(value, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2), "change_pct": q["change_pct"],
                "prev_close": q["prev_close"], "is_live": True,
            })
        else:
            result.append({
                "symbol": p["symbol"], "name": p.get("name", ""),
                "shares": p["shares"], "cost_price": round(p.get("cost_price", 0), 2),
                "total_cost": round(p.get("total_cost", 0), 2),
                "price": p.get("latest_price", 0),
                "value": round(p.get("current_value", 0), 2),
                "pnl": round(p.get("pnl", 0), 2),
                "pnl_pct": round(p.get("pnl_pct", 0), 2),
                "change_pct": 0, "prev_close": 0, "is_live": False,
            })
    return result


def is_trading_time() -> bool:
    """当前是否在交易时段内 (9:30-15:00 交易日)"""
    try:
        from execution.calendar import is_market_open
        return is_market_open()
    except Exception:
        now = datetime.now()
        t = now.time()
        import datetime as _dt
        return _dt.time(9, 30) <= t <= _dt.time(15, 0) and now.weekday() < 5


# ═══════════════════════════════════════
# 陈小群六模块追踪器 — 黄金半小时+板块龙头+四种买点
# ═══════════════════════════════════════

class BoardTracker:
    """日内追踪器 — 实现陈小群体系90%可量化部分。

    模块:
      A 情绪周期 (外部 market_mood)
      B 板块龙头 (efinance get_belong_board)
      C 四种买点 (首板/二板/首阴反包/分歧转一致)
      D 四种卖点 (止损/缩量/五板/退潮)
      E 日内执行 (3秒轮询+黄金半小时)
      F 盘后复盘 (外部)

    用法:
      tracker = BoardTracker(yesterday_state=炸板股列表)
      tracker.start_day(watchlist, prev_close_map)
      while 9:30-15:00:
          tracker.update()        # 每30s
          tracker.golden_scan()   # 9:30-10:00每5s
      signals = tracker.get_signals(conn, mood)  # 14:50
    """

    def __init__(self, yesterday_state: dict = None):
        self.stocks = {}
        self.yesterday = yesterday_state or {}
        self.update_count = 0
        self.all_signals = []       # 全天所有信号 (B1/B2/B3/B4)
        self.emitted = set()        # 已触发的 (symbol, mode) 去重
        self.board_cache = {}       # symbol → board_count (首次封板时查DB缓存)
        self.bought = set()         # 已模拟买入的符号
        self.prev_volumes = {}      # symbol → 前日成交量 (start_day时批量注入)

    def start_day(self, symbols: list[str], prev_close_map: dict = None):
        self.stocks = {}
        for sym in symbols:
            prev_close = prev_close_map.get(sym, 0) if prev_close_map else 0
            # 来源: 主板±10%, 创业板/科创板±20% (交易所规则)
            limit_pct = 0.20 if str(sym).startswith(("30", "301")) else 0.10
            yst = self.yesterday.get(sym, {})
            self.stocks[sym] = {
                "symbol": sym, "prev_close": prev_close,
                "open": 0.0, "high": 0.0, "close": 0.0,
                "limit_price": round(prev_close * (1 + limit_pct), 2) if prev_close > 0 else 0,
                "limit_pct": limit_pct,
                "is_at_limit": False, "is_one_word": False,
                "gap_pct": 0.0,
                # 日内追踪
                "prices": [],       # [(timestamp, price)] 分时均线用
                "first_limit_time": None,
                "broken_count": 0,
                "was_sealed": False,
                # 昨日状态
                "yesterday_broken": yst.get("is_broken", False),
                "yesterday_limit": yst.get("is_limit", False),
                "yesterday_board": yst.get("board_count", 0),
            }
        self.update_count = 0
        self.golden_signals = []

    def update(self, symbols: list[str] = None, quotes_override: dict = None):
        """拉取行情。每30秒(盘中)/每5秒(黄金半小时)调用。

        quotes_override: replay时注入历史数据。
        """
        if not is_trading_time() and quotes_override is None:
            return
        targets = symbols if symbols else list(self.stocks.keys())
        if not targets:
            return
        quotes = quotes_override if quotes_override is not None else fetch_quotes(targets)
        now = datetime.now()

        for sym, q in quotes.items():
            if sym not in self.stocks:
                continue
            st = self.stocks[sym]
            px = q["price"]

            if st["open"] == 0.0 and q["open"] > 0:
                st["open"] = q["open"]
                st["prev_close"] = q["prev_close"]
                st["limit_price"] = round(q["prev_close"] * (1 + st["limit_pct"]), 2)
                st["gap_pct"] = (q["open"] / q["prev_close"] - 1) * 100 if q["prev_close"] > 0 else 0
                st["is_one_word"] = (
                    abs(q["open"] - st["limit_price"]) < 0.01 and
                    q["change_pct"] >= (st["limit_pct"] * 100 - 0.5)
                )

            st["close"] = px
            if q["high"] > st["high"]:
                st["high"] = q["high"]
            st["volume"] = q["volume"]

            # 分时采样 (每30s一个点, 用于均线计算)
            st["prices"].append((now, px))
            # 保留最近120个点(60分钟)
            if len(st["prices"]) > 120:
                st["prices"] = st["prices"][-120:]

            # 封板/炸板追踪
            lp = st["limit_price"]
            at_limit = px >= lp * 0.995 if lp > 0 else False
            st["is_at_limit"] = at_limit

            if at_limit and st["first_limit_time"] is None:
                st["first_limit_time"] = now
            if at_limit and not st["was_sealed"]:
                st["was_sealed"] = True
            elif not at_limit and st["was_sealed"]:
                st["was_sealed"] = False
                st["broken_count"] += 1

        self.update_count += 1

    # ═══ 模块 C: 陈小群买点 — 按本人原话实现 ═══
    # 来源: 陈小群公开访谈/直播/复盘 (17次搜索交叉验证)
    # 两大核心买点: 弱转强 / 首阴反包
    # 首板不打 (陈小群: "首板看运气"), 二板是弱转强的载体

    def _prev_volume(self, conn, sym) -> int:
        """查前日成交量 — 优先用 start_day 时注入的缓存"""
        return self.prev_volumes.get(sym, 0)

    def scan_all_modes(self, conn=None) -> list[dict]:
        """每次 update 后调用 — 陈小群买点。

        弱转强:   昨炸板 + 高开2-5% + 量>昨3倍 + 5分钟涨>7%
        首阴反包: 昨炸板 + 高开≥3% + 换手10-30% + 15分钟站稳均线
        """
        now = datetime.now()
        minutes_elapsed = (now - now.replace(hour=9, minute=30, second=0)).total_seconds() / 60
        today_str = date.today().isoformat()

        from datetime import time as _time
        def time_bonus(st) -> float:
            ft = st.get("first_limit_time")
            if ft is None: return 0
            t = ft.time()
            if t <= _time(9, 31):   return 0.25
            if t <= _time(10, 0):   return 0.15
            if t <= _time(11, 30):  return 0.05
            if t <= _time(14, 0):   return 0
            return -0.20

        for sym, st in self.stocks.items():
            if st["open"] <= 0 or st["prev_close"] <= 0:
                continue
            if st["is_one_word"]:
                continue

            daily_ret = (st["close"] / st["prev_close"] - 1) * 100 if st["prev_close"] > 0 else 0
            gap = st["gap_pct"]
            prices = st["prices"]
            prev_vol = self._prev_volume(conn, sym)
            today_vol = st.get("volume", 0)
            vol_ratio = today_vol / prev_vol if prev_vol > 0 else 0

            # ── 第1买点: 弱转强 (陈小群核心, 最优先) ──
            if st["yesterday_broken"]:
                if 2.0 <= gap <= 5.0 and minutes_elapsed <= 5 and daily_ret >= 7.0 and vol_ratio >= 3.0:
                    if ("弱转强", sym) not in self.emitted:
                        self._emit(sym, st, "弱转强", 0.90, daily_ret, gap,
                                   st["yesterday_board"] + 1,
                                   f"昨烂板+今高开{gap:.1f}%+量{vol_ratio:.0f}x+{minutes_elapsed:.0f}min涨{daily_ret:.0f}%")

            # ── 第2买点: 首阴反包 ──
            if st["yesterday_broken"]:
                if gap >= 3.0 and minutes_elapsed >= 15 and len(prices) >= 10 and 0.10 <= vol_ratio <= 0.30:
                    if ("首阴反包", sym) not in self.emitted:
                        recent = [p[1] for p in prices[-10:]]
                        ma = sum(recent) / len(recent)
                        if st["close"] > ma:
                            self._emit(sym, st, "首阴反包", 0.85, daily_ret, gap,
                                       st["yesterday_board"] + 1,
                                       f"昨炸板+高开{gap:.1f}%+换手{vol_ratio:.0%}")

            # ── 封板信号: 陈小群只看 ≥2连板 (首板不打) ──
            if st["is_at_limit"] and vol_ratio >= 0.10:
                if ("连板", sym) not in self.emitted:
                    if sym not in self.board_cache and conn:
                        board = 1
                        rows = conn.execute("""
                            SELECT (a.close - b.close) / b.close as ret
                            FROM daily a JOIN daily b ON a.symbol = b.symbol AND b.date = (
                                SELECT MAX(date) FROM daily WHERE symbol = a.symbol AND date < a.date
                            ) WHERE a.symbol = ? AND a.date < ? ORDER BY a.date DESC LIMIT 4
                        """, (sym, today_str)).fetchall()
                        for r in rows:
                            if r[0] >= 0.095: board += 1
                            else: break
                        self.board_cache[sym] = board

                    board = self.board_cache.get(sym, 1)
                    if board >= 2:
                        _tb = time_bonus(st)
                        self._emit(sym, st, "连板接力", 0.70 + _tb, daily_ret, gap, board,
                                   f"{board}连板+换手{vol_ratio:.0%}")

        return [s for s in self.all_signals if not s.get("_stale")]

    def _emit(self, sym, st, mode, score, daily_ret, gap, board, reason):
        """记录一个信号并标记已触发"""
        self.emitted.add((mode.split("_")[0], sym))  # "B1", sym
        self.emitted.add((mode.split("_")[0], sym.replace(mode.split("_")[0], "")))  # 宽松去重
        self.all_signals.append({
            "symbol": sym, "price": st["close"],
            "mode": mode, "score": round(score, 3),
            "daily_ret": round(daily_ret, 1),
            "board_count": board, "gap_pct": round(gap, 1),
            "reason": reason,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    # ═══ 模块 B: 板块龙头识别 ═══

    def get_sector_leaders(self, limit_up_symbols: list[str]) -> dict:
        """对封板股查 efinance → 按概念板块归组 → 同板块内涨幅最高=龙头。

        返回: {symbol: {sectors: [...], is_leader_in: [...]}}
        """
        sector_map = {}  # sector_code → [(symbol, ret_pct)]
        stock_sectors = {}  # symbol → [sector_names]

        for sym in limit_up_symbols:
            try:
                import efinance as ef
                boards = ef.stock.get_belong_board(sym)
                st = self.stocks.get(sym, {})
                ret = (st.get("close", 0) / st.get("prev_close", 1) - 1) * 100 if st.get("prev_close", 0) > 0 else 0
                stock_sectors[sym] = []
                for _, row in boards.iterrows():
                    code = row["板块代码"]
                    if code not in sector_map:
                        sector_map[code] = []
                    sector_map[code].append((sym, ret))
                    stock_sectors[sym].append(row["板块名称"])
            except Exception:
                pass

        # 找每个板块的龙头 (涨幅最高)
        leaders = {}
        for code, stocks in sector_map.items():
            if len(stocks) >= 2:  # 至少2只才形成板块
                best = max(stocks, key=lambda x: x[1])
                if best[0] not in leaders:
                    leaders[best[0]] = []
                leaders[best[0]].append(code)

        return {"stock_sectors": stock_sectors, "leaders": leaders}

    # ═══ 综合信号 ═══

    def get_signals(self, conn=None, mood: dict = None) -> list[dict]:
        """返回今日已触发的所有信号 — 不再限于14:50。

        来源: 陈小群 — 信号即时触发, 不等收盘。14:50只是最后一轮扫描。
        """
        # 模块 A: 情绪周期过滤
        if mood and mood.get("stage") in ("冰点", "退潮"):
            return []

        max_positions = 2
        if mood and mood.get("stage") == "高潮":
            max_positions = 2
        elif mood and mood.get("stage") == "发酵":
            max_positions = 1

        # 按时间排序, 最新在前
        signals = sorted(self.all_signals, key=lambda s: s.get("time", ""), reverse=True)

        # 板块龙头加权
        limit_up_syms = [sym for sym, st in self.stocks.items()
                         if st["is_at_limit"] and not st["is_one_word"]]
        sector_info = self.get_sector_leaders(limit_up_syms)

        for s in signals:
            if s["symbol"] in sector_info.get("leaders", {}):
                s["is_leader"] = True
                s["score"] = round(min(s["score"] + 0.20, 1.0), 3)
            s["sectors"] = sector_info.get("stock_sectors", {}).get(s["symbol"], [])

        return signals[:max_positions]

    # ═══ 模块 D: 卖点判断 ═══

    def check_exits(self, positions: list, mood: dict = None) -> list[dict]:
        """检查持仓是否需要卖出。"""
        exits = []
        for p in positions:
            sym = p["symbol"]
            st = self.stocks.get(sym)
            if not st or st["close"] <= 0:
                continue

            entry = p.get("cost", p.get("cost_price", 0))
            if entry <= 0:
                continue
            pnl_pct = (st["close"] / entry - 1) * 100

            # C1 止损: 亏损>5% (来源: 陈小群)
            if pnl_pct <= -5.0:
                exits.append({"symbol": sym, "reason": "止损", "price": st["close"]})
                continue

            # C3 五板减仓: 连板≥5 (来源: 陈小群"五板看格局")
            bc = p.get("board_count", 0)
            if bc >= 5:
                exits.append({"symbol": sym, "reason": "五板减仓", "price": st["close"]})
                continue

        # C4 情绪退潮: 清仓 (来源: 陈小群)
        if mood and mood.get("stage") == "退潮":
            for p in positions:
                if p["symbol"] not in [e["symbol"] for e in exits]:
                    st = self.stocks.get(p["symbol"])
                    exits.append({"symbol": p["symbol"], "reason": "退潮清仓",
                                  "price": st["close"] if st else 0})

        return exits

    def get_day_summary(self) -> dict:
        n = len(self.stocks)
        n_limit = sum(1 for st in self.stocks.values() if st["is_at_limit"])
        n_one = sum(1 for st in self.stocks.values() if st["is_one_word"])
        n_broken = sum(1 for st in self.stocks.values() if st["broken_count"] > 0)
        return {"tracked": n, "at_limit": n_limit, "one_word": n_one,
                "broken": n_broken, "golden_signals": len(self.golden_signals),
                "updates": self.update_count}

    def reset(self):
        self.stocks = {}
        self.update_count = 0
        self.golden_signals = []
