"""日内连板监控 v2 — 陈小群六模块体系。

模块 A: 情绪周期(外部market_mood) → 仓位控制
模块 B: 板块龙头(efinance) → 龙头加权
模块 C: 四种买点(BoardTracker)
模块 D: 四种卖点(BoardTracker)
模块 E: 日内执行(3秒黄金半小时 + 30秒盘中)
模块 F: 盘后复盘(待实现)

用法: PYTHONPATH=. python3 intraday_runner.py
"""

import time, sqlite3, os
from datetime import datetime, date, timedelta
from config.loader import get as cfg
from execution.quote import BoardTracker, is_trading_time, fetch_quotes
from execution.calendar import is_trading_day
from utils.logger import get_logger

logger = get_logger("intraday.runner")
DB = "data/market.db"
TRADE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trades.db")


def init_trade_db():
    """首次运行自动创建交易记录表。来源: 每笔模拟交易永久存储, 不可丢失。"""
    conn = sqlite3.connect(TRADE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            price REAL NOT NULL, shares INTEGER NOT NULL,
            board_count INTEGER DEFAULT 0,
            pnl REAL, pnl_pct REAL, capital_after REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sim_date ON sim_trades(date);
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            mode TEXT NOT NULL,
            price REAL,
            score REAL,
            board_count INTEGER DEFAULT 0,
            gap_pct REAL,
            daily_ret REAL,
            reason TEXT,
            is_bought INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
        CREATE INDEX IF NOT EXISTS idx_signals_mode ON signals(date, mode);
    """)
    conn.commit()
    conn.close()


def record_trade(date_str, symbol, side, price, shares, board_count=0, pnl=None, pnl_pct=None, capital_after=None):
    """写入永久交易记录。"""
    conn = sqlite3.connect(TRADE_DB)
    conn.execute("""INSERT INTO sim_trades (date, symbol, side, price, shares, board_count, pnl, pnl_pct, capital_after)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (date_str, symbol, side, price, shares, board_count, pnl, pnl_pct, capital_after))
    conn.commit()
    conn.close()


def record_signal(date_str, time_str, symbol, mode, price, score=0, board_count=0, gap_pct=0, daily_ret=0, reason=""):
    """持久化信号 — 无论是否成交，全部留存用于复盘。"""
    conn = sqlite3.connect(TRADE_DB)
    cur = conn.execute("""INSERT INTO signals (date, time, symbol, mode, price, score, board_count, gap_pct, daily_ret, reason)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (date_str, time_str, symbol, mode, price, score, board_count, gap_pct, daily_ret, reason))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


from web.shared import update_state, get_state


def get_watchlist(conn) -> list:
    """全市场监控池 — 所有主板+创业板最新交易日有交易的股票。"""
    rows = conn.execute("""
        SELECT DISTINCT symbol FROM daily
        WHERE date = (SELECT MAX(date) FROM daily WHERE date LIKE '%-%-%')
          AND symbol NOT LIKE '688%' AND symbol NOT LIKE '8%'
          AND symbol NOT LIKE '4%' AND symbol NOT LIKE '92%'
          AND symbol NOT LIKE '900%'
    """).fetchall()
    return [r[0] for r in rows]


def load_yesterday_state(conn) -> dict:
    """加载昨日涨停/炸板状态 — 用于 B3/B4 信号检测。

    返回: {symbol: {is_limit, is_broken, board_count}}
    """
    rows = conn.execute("""
        SELECT a.symbol, a.close, a.high, a.open,
               b.close as prev_close,
               (a.close - b.close) / b.close as ret
        FROM daily a
        JOIN daily b ON a.symbol = b.symbol AND b.date = (
            SELECT MAX(date) FROM daily WHERE symbol = a.symbol AND date < a.date
        )
        WHERE a.date = (
            SELECT MAX(date) FROM daily WHERE date < DATE('now') AND date LIKE '%-%-%'
        )
        AND b.close > 0 AND a.close > 0
    """).fetchall()

    state = {}
    for r in rows:
        sym, close, high, open_, prev_close, ret = r
        limit_pct = 0.20 if str(sym).startswith(("30", "301")) else 0.10
        limit_price = prev_close * (1 + limit_pct)
        is_limit = ret >= 0.095 if ret else False
        is_broken = (high >= limit_price * 0.995) and (close < limit_price * 0.995)
        state[sym] = {"is_limit": bool(is_limit), "is_broken": bool(is_broken),
                       "board_count": 1}  # board_count 在 get_signals 里重算
    return state


def run():
    """全天候运行 — 非交易时间休眠, 交易日自动激活。"""
    init_trade_db()
    logger.info("日内监控启动, 全天候运行中...")
    update_state({"status": "休市"})

    # 持久化: 避免重启重复同步
    import json as _json
    _sync_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".sync_state")
    try:
        with open(_sync_state_file) as f:
            last_sync_date = date.fromisoformat(_json.load(f).get("date", ""))
    except Exception:
        last_sync_date = None

    while True:
        # ── 每日日线同步 (只在盘前, 盘中直接跳过) ──
        if last_sync_date != date.today() and now.hour < 9:
            try:
                from data.store import DataStore
                n = DataStore(db_path=DB).update_daily(start=(date.today() - timedelta(days=2)).isoformat())
                if n > 0:
                    logger.info(f"日线同步: +{n} 条")
            except Exception:
                pass
            last_sync_date = date.today()
            try:
                with open(_sync_state_file, "w") as f:
                    _json.dump({"date": last_sync_date.isoformat()}, f)
            except Exception:
                pass
        now = datetime.now()

        # 非交易日 → 等明天
        if not is_trading_day(date.today()):
            update_state({"status": "休市", "progress": "", "all_signals": [], "final_signals": [], "golden_signals": [], "mood": {}, "summary": {}})
            logger.info(f"{date.today()} 非交易日, 休眠至明早8:00")
            tomorrow = now.replace(hour=8, minute=0, second=0) + timedelta(days=1)
            wait = (tomorrow - now).total_seconds()
            if wait > 0 and wait < 86400:
                time.sleep(wait)
            else:
                time.sleep(3600)
            continue

        # 盘前等待 → 到 9:25
        if now.hour < 9 or (now.hour == 9 and now.minute < 25):
            update_state({"status": "盘前", "progress": "等待开盘", "all_signals": [], "final_signals": [], "golden_signals": [], "mood": {}, "summary": {}})
            target = now.replace(hour=9, minute=25, second=0)
            wait = (target - now).total_seconds()
            if wait > 0:
                logger.info(f"等待开盘... ({wait:.0f}s)")
                time.sleep(min(wait, 300))
            continue

        # 午休: 11:30-13:00
        if (now.hour == 11 and now.minute >= 30) or (now.hour == 12):
            update_state({"status": "午休", "all_signals": [], "final_signals": [], "golden_signals": []})
            target = now.replace(hour=13, minute=0, second=0)
            wait = (target - now).total_seconds()
            if wait > 0:
                logger.info(f"午休中... ({wait:.0f}s)")
                time.sleep(min(wait, 300))
            continue

        # 盘后 → 等明天
        if now.hour >= 15:
            update_state({"status": "已收盘", "all_signals": [], "final_signals": [], "golden_signals": []})
            logger.info("已收盘, 休眠至明早8:00")
            tomorrow = now.replace(hour=8, minute=0, second=0) + timedelta(days=1)
            time.sleep((tomorrow - now).total_seconds())
            continue

        # ═══ 交易时段内: 初始化并监控 ═══
        logger.info(f"=== {date.today()} 开始监控 ===")
        update_state({"status": "盘中", "progress": "加载行情..."})
        conn = sqlite3.connect(DB)
        watchlist = get_watchlist(conn)
        yesterday = load_yesterday_state(conn)

        # 来源: 一条SQL替代4500条查询 — 昨收价批量加载
        rows = conn.execute("""
            SELECT symbol, close, volume FROM daily
            WHERE date = (SELECT MAX(date) FROM daily WHERE date < DATE('now') AND date LIKE '%-%-%')
        """).fetchall()
        prev_close_map = {r[0]: r[1] for r in rows if r[1]}
        prev_volume_map = {r[0]: r[2] for r in rows if r[2]}
        logger.info(f"昨收价加载: {len(prev_close_map)} 只 — 昨日量: {len(prev_volume_map)} 只")

        # ── G1/G3: 情绪周期 (来源: 陈小群——情绪决定仓位系数) ──
        mood = {"stage": "复苏", "coefficient": 0.5}
        is_retreat = False
        try:
            from factor.market_mood import smooth_stage as detect_mood
            import pandas as pd
            raw = pd.read_sql_query(
                "SELECT symbol, date, close FROM daily WHERE date >= DATE('now', '-30 days')", conn)
            close_df = raw.pivot(index="date", columns="symbol", values="close") if len(raw) > 0 else pd.DataFrame()
            mood = detect_mood(close_df) if not close_df.empty else mood
            is_retreat = mood.get("stage", "") in ("退潮", "冰点")
            logger.info(f"情绪: {mood['stage']} 系数={mood['coefficient']:.0%} 退潮={is_retreat}")
        except Exception:
            pass

        # ── G2: 连亏空仓 (来源: 陈小群——连亏2笔强制空仓3天) ──
        freeze_until = None
        try:
            tc3 = sqlite3.connect(TRADE_DB)
            last_sells = tc3.execute(
                "SELECT pnl FROM sim_trades WHERE side='sell' ORDER BY id DESC LIMIT 3").fetchall()
            loss_threshold = -get_state()["total_asset"] * 0.05
            if len(last_sells) >= 2 and all(r[0] is not None and r[0] < loss_threshold for r in last_sells[:2]):
                freeze_until = date.today() + timedelta(days=3)
                logger.warning(f"  连亏{len([r for r in last_sells if r[0] and r[0]<=0])}笔, 空仓至 {freeze_until}")
            tc3.close()
        except Exception:
            pass

        # ── 从 trades.db 恢复状态 (唯一真相源) ──
        capital = float(cfg("backtest.initial_capital", 5000))
        positions = []
        trades_list = []
        try:
            tc = sqlite3.connect(TRADE_DB)
            # 计算可用资金: 5000 - 买入总支出 + 卖出总收入 (来源: 逐笔复算)
            capital = float(cfg("backtest.initial_capital", 5000))
            all_trades = tc.execute("SELECT side, price, shares FROM sim_trades ORDER BY id").fetchall()
            for side, price, shares in all_trades:
                val = price * shares
                if side == "buy":
                    capital -= val + max(val * 0.0003, 5)
                else:
                    capital += val - max(val * 0.0003, 5) - val * 0.001
            # 未卖出的持仓
            pos_rows = tc.execute("""
                SELECT symbol, price, shares, board_count, date FROM sim_trades
                WHERE side='buy' AND symbol NOT IN (
                    SELECT symbol FROM sim_trades WHERE side='sell'
                )
            """).fetchall()

            # ── A3: MA5 (来源: 陈小群——5日线是生命线, 仅算持仓股) ──
            ma5_map = {}
            try:
                for r in pos_rows:
                    close_rows = conn.execute(
                        "SELECT close FROM daily WHERE symbol=? AND date < DATE('now') ORDER BY date DESC LIMIT 5",
                        (r[0],)
                    ).fetchall()
                    if len(close_rows) >= 5:
                        ma5_map[r[0]] = sum(row[0] for row in close_rows) / len(close_rows)
            except Exception:
                pass  # MA5 失败不阻塞持仓恢复
            # 合并同一股票的多笔买入 (加权平均成本)
            _merged = {}
            for r in pos_rows:
                sym, cost_price, shares, board, buy_date = r[0], r[1], r[2], r[3], r[4]
                # 今日买入的 → 合并
                if buy_date >= date.today().isoformat():
                    if sym in _merged:
                        m = _merged[sym]
                        total_sh = m["shares"] + shares
                        m["price"] = round((m["price"] * m["shares"] + cost_price * shares) / total_sh, 2)
                        m["shares"] = total_sh
                        m["board_count"] = max(m["board_count"], board)
                        m["date"] = min(m["date"], buy_date)
                    else:
                        _merged[sym] = {"symbol": sym, "price": cost_price, "shares": shares,
                                       "board_count": board, "date": buy_date,
                                       "has_sealed": False, "break_count": 0, "was_at_limit": False}
                    continue

                # 昨日持仓 → 条件卖出
                days_held = (date.today() - date.fromisoformat(buy_date)).days
                today_open = prev_close_map.get(sym, cost_price)
                prev_close = prev_close_map.get(sym, cost_price)
                ma5 = ma5_map.get(sym, 0)
                sell_reason = None

                # A4: 退潮清仓 (来源: 陈小群——退潮必须空仓)
                if is_retreat:
                    sell_reason = f"退潮清仓({mood.get('stage','')})"

                # A3: MA5破位 (来源: 陈小群——5日线是生命线)
                elif ma5 > 0 and prev_close < ma5 and today_open < ma5:
                    sell_reason = f"MA5破位(昨收{prev_close:.2f}<MA5{ma5:.2f})"

                # A2: 时间止损——跟风股 (来源: 陈小群——2天不达预期)
                elif board < 3 and days_held >= 2:
                    sell_reason = f"时间止损(跟风{days_held}天)"

                # A1: 低开闪卖 (来源: 陈小群——低开>3%+5分不翻红)
                elif today_open < prev_close * 0.97 and today_open > 0:
                    sell_reason = f"低开闪卖(-{(1-today_open/prev_close)*100:.0f}%)"

                if sell_reason:
                    sell_val = shares * today_open
                    fee = max(sell_val * 0.0003, 5) + sell_val * 0.001
                    pnl = sell_val - shares * cost_price - fee
                    capital += sell_val - fee
                    record_trade(date.today().isoformat(), sym, "sell", today_open,
                                shares, board, round(pnl, 2),
                                round((today_open/cost_price-1)*100, 2), round(capital, 2))
                    logger.info(f"  🔴 {sell_reason} {sym}: ¥{cost_price:.2f}→¥{today_open:.2f} PnL=¥{pnl:.0f}")
                else:
                    # A5: 持有 → 合并
                    if sym in _merged:
                        m = _merged[sym]
                        total_sh = m["shares"] + shares
                        m["price"] = round((m["price"] * m["shares"] + cost_price * shares) / total_sh, 2)
                        m["shares"] = total_sh
                        m["board_count"] = max(m["board_count"], board)
                        m["date"] = min(m["date"], buy_date)
                    else:
                        _merged[sym] = {"symbol": sym, "price": cost_price, "shares": shares,
                                       "board_count": board, "date": buy_date,
                                       "has_sealed": False, "break_count": 0, "was_at_limit": False}
                    logger.info(f"  🟢 继续持有 {sym}: cost=¥{cost_price:.2f} {board}连板 {days_held}天")
            positions = list(_merged.values())
            # 加载所有历史交易
            all_trades = tc.execute("SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades ORDER BY id").fetchall()
            trades_list = [{"date": t[0], "symbol": t[1], "side": t[2], "price": t[3],
                           "shares": t[4], "pnl": t[5], "pnl_pct": t[6]} for t in all_trades]
            tc.close()
        except Exception:
            pass

        update_state({"status": "盘中", "progress": "初始化追踪器..."})
        tracker = BoardTracker(yesterday_state=yesterday)
        tracker.start_day(list(prev_close_map.keys()), prev_close_map)
        tracker.prev_volumes = prev_volume_map  # 昨日量缓存
        # 恢复已买入状态 (从今日交易记录中读取)
        try:
            tc2 = sqlite3.connect(TRADE_DB)
            today_buys = tc2.execute(
                "SELECT symbol FROM sim_trades WHERE side='buy' AND date=?",
                (date.today().isoformat(),)
            ).fetchall()
            for (sym,) in today_buys:
                tracker.bought.add(sym)  # 今天已买入, 不再重复买
            tc2.close()
        except Exception:
            pass
        # G2+G3: 买前拦截 (来源: 陈小群——退潮不买/连亏空仓)
        can_buy = not is_retreat and (freeze_until is None or date.today() >= freeze_until)
        if not can_buy:
            reason = "退潮" if is_retreat else f"连亏空仓至{freeze_until}"
            logger.warning(f"  🚫 禁止买入: {reason}")

        logger.info(f"追踪 {len(tracker.stocks)} 只股票 | 本金 ¥{capital:,.0f} | 仓位系数{mood['coefficient']:.0%} | {'可买' if can_buy else '禁买'}")

        pos_value_init = sum(p["shares"] * p["price"] for p in positions)
        positions_init = [{
            "symbol": p["symbol"], "name": "", "shares": p["shares"],
            "price": p["price"], "current": p["price"],
            "board_count": p.get("board_count", 0),
            "date": p.get("date", ""), "has_sealed": p.get("has_sealed", False),
            "pnl_pct": 0, "value": round(p["shares"] * p["price"], 2),
        } for p in positions]
        update_state({"status": "盘中", "progress": "拉取实时行情...", "capital": round(capital, 2),
                     "total_asset": round(capital + pos_value_init, 2),
                     "pos_value": round(pos_value_init, 2), "mood": mood,
                     "positions": positions_init})

        last_sector_scan = None  # 模块B: 板块龙头扫描间隔
        last_heartbeat = None

        while is_trading_time():
            now = datetime.now()
            tracker.update()

            # 心跳: 每5分钟确认存活
            if last_heartbeat is None or (now - last_heartbeat).total_seconds() >= 300:
                logger.info(f"💓 {now.strftime('%H:%M')} | 信号{len(tracker.all_signals)} | 持仓{len(positions)} | ¥{capital:,.0f}")
                last_heartbeat = now
            new_signals = tracker.scan_all_modes(conn=conn)

            # ── 持仓快速刷新 (每轮3-5s, 补充tracker+名称) ──
            if positions:
                try:
                    pos_quotes = fetch_quotes([p["symbol"] for p in positions])
                    for p in positions:
                        q = pos_quotes.get(p["symbol"])
                        if q:
                            p["name"] = q.get("name", "")
                        if q:
                            sym = p["symbol"]
                            if sym not in tracker.stocks:
                                tracker.stocks[sym] = {
                                    "symbol": sym, "close": q["price"],
                                    "open": q.get("open", q["price"]),
                                    "high": q.get("high", q["price"]),
                                    "prev_close": q.get("prev_close", p["price"]),
                                    "is_at_limit": False, "is_one_word": False,
                                    "broken_count": 0, "was_sealed": False,
                                    "first_limit_time": None, "gap_pct": 0,
                                    "volume": q.get("volume", 0), "prices": [],
                                    "limit_price": 0, "limit_pct": 0.10,
                                    "yesterday_broken": False, "yesterday_limit": False,
                                    "yesterday_board": 0,
                                }
                            else:
                                tracker.stocks[sym]["close"] = q["price"]
                    logger.info("📊 持仓刷新: %s", [(p['symbol'], tracker.stocks[p['symbol']]['close']) for p in positions if p['symbol'] in tracker.stocks])
                except Exception as e:
                    logger.warning("📊 持仓刷新失败: %s", e)

            # ── 模块B: 板块龙头扫描 (每5分钟, efiance API慢) ──
            if last_sector_scan is None or (now - last_sector_scan).total_seconds() >= 300:
                try:
                    limit_up_syms = [sym for sym, st in tracker.stocks.items()
                                     if st["is_at_limit"] and not st["is_one_word"]]
                    if len(limit_up_syms) >= 2:
                        sector_info = tracker.get_sector_leaders(limit_up_syms)
                        leaders = sector_info.get("leaders", {})
                        # 给龙头信号加分
                        for s in new_signals:
                            if s["symbol"] in leaders:
                                s["is_leader"] = True
                                s["score"] = round(min(s["score"] + 0.20, 1.0), 3)
                            s["sectors"] = sector_info.get("stock_sectors", {}).get(s["symbol"], [])
                        last_sector_scan = now
                except Exception:
                    pass

            # 按得分降序排列 → 龙头优先
            new_signals.sort(key=lambda s: s.get("score", 0), reverse=True)

            # 持久化新信号 (去重: 同日+同symbol+同mode只存一次)
            today_str = date.today().isoformat()
            sig_conn = sqlite3.connect(TRADE_DB)
            existing = set(
                sig_conn.execute("SELECT symbol, mode FROM signals WHERE date=?", (today_str,)).fetchall()
            )
            for s in new_signals:
                if (s["symbol"], s["mode"]) not in existing:
                    record_signal(today_str, s.get("time", ""), s["symbol"], s["mode"],
                                 s.get("price", 0), s.get("score", 0), s.get("board_count", 0),
                                 s.get("gap_pct", 0), s.get("daily_ret", 0), s.get("reason", ""))
                    existing.add((s["symbol"], s["mode"]))
            sig_conn.close()

            # D: 更新持仓元数据 (封板/炸板追踪)
            for pos in positions:
                sym = pos["symbol"]
                st = tracker.stocks.get(sym)
                if st:
                    if st["is_at_limit"]:
                        pos["has_sealed"] = True
                    pos["break_count"] = st["broken_count"]
                    pos["was_at_limit"] = st["is_at_limit"]

            # G4: 双跌停禁买 (来源: 陈小群——≥2只跌停不做)
            # G5: 成交量萎缩禁买 (来源: 陈小群——大盘连缩3天>20%不做)
            buy_blocked = False
            try:
                # 近3日成交量
                vols = conn.execute("""
                    SELECT SUM(volume) FROM daily
                    WHERE date >= DATE('now', '-3 days') AND date < DATE('now')
                    GROUP BY date ORDER BY date
                """).fetchall()
                if len(vols) >= 3:
                    v = [r[0] for r in vols]
                    if v[0] > 0 and v[-1] < v[0] * 0.8:
                        buy_blocked = True
                        logger.warning(f"  🚫 成交量萎缩禁买: {v[-1]/v[0]*100:.0f}%")
            except Exception:
                pass
            if can_buy:
                try:
                    limit_downs = [sym for sym, st in tracker.stocks.items()
                                   if st.get("prev_close", 0) > 0 and st.get("close", 0) > 0
                                   and (st["close"] / st["prev_close"] - 1) <= -0.095]
                    if len(limit_downs) >= 2:
                        buy_blocked = True
                        logger.warning(f"  🚫 双跌停禁买: {len(limit_downs)}只跌停")
                except Exception:
                    pass

            # 新信号 → 买入 (陈小群: 不补仓, 持仓≤3只, 全仓最优)
            max_positions = int(cfg("backtest.max_positions", 3))
            if can_buy and not buy_blocked:
              for s in new_signals:
                if len(positions) >= max_positions:
                    break
                sym, mode = s["symbol"], s["mode"]
                # ── 陈小群铁律: 不补仓, 已有持仓跳过 ──
                if any(p["symbol"] == sym for p in positions):
                    continue
                # ── ST/*ST/退市过滤 ──
                try:
                    name = fetch_quotes([sym]).get(sym, {}).get("name", "")
                    if "ST" in name or "退" in name:
                        continue
                except Exception:
                    pass
                entry_px = s["price"]
                max_lots = int(capital / (entry_px * 100 + max(entry_px * 100 * 0.0003, 5)))
                if max_lots < 1:
                    continue
                shares = max_lots * 100
                cost = shares * entry_px
                fee = max(cost * 0.0003, 5)
                capital -= (cost + fee)
                positions.append({"symbol": sym, "price": entry_px, "shares": shares,
                                 "date": today_str, "board_count": s.get("board_count", 0),
                                 "has_sealed": True, "break_count": 0, "was_at_limit": True})
                trades_list.append({"symbol": sym, "side": "buy", "price": entry_px, "shares": shares, "date": today_str})
                record_trade(today_str, sym, "buy", entry_px, shares,
                            s.get("board_count", 0), capital_after=round(capital, 2))
                sid_conn = sqlite3.connect(TRADE_DB)
                sid_conn.execute(
                    "UPDATE signals SET is_bought=1 WHERE id=(SELECT id FROM signals WHERE symbol=? AND date=? AND is_bought=0 ORDER BY id DESC LIMIT 1)",
                    (sym, today_str))
                sid_conn.commit()
                sid_conn.close()
                logger.info(f"  💰 买入 {sym} ({mode}): ¥{entry_px:.2f} × {shares}股 余额¥{capital:.0f}")

            # ── B1-B3: 盘中风控 (来源: 陈小群卖出纪律) ──
            for pos in list(positions):
                # T+1: 今天买的不能卖 (来源: A股交易规则)
                if pos.get("date", "") >= date.today().isoformat():
                    continue
                sym = pos["symbol"]
                st = tracker.stocks.get(sym)
                if not st or st["close"] <= 0:
                    continue
                sell_reason = None
                pnl_pct = (st["close"] / pos["price"] - 1) * 100

                # B1: 硬止损 -5% (最高优先)
                if pnl_pct <= -5:
                    sell_reason = f"止损({pnl_pct:.1f}%)"

                # B2: 尾盘炸板 (14:30后+封板→炸板)
                elif now.hour >= 14 and now.minute >= 30:
                    if pos.get("was_at_limit") and not st["is_at_limit"] and pos.get("has_sealed"):
                        sell_reason = f"尾盘炸板"

                # B3: 反复烂板 (≥3次)
                elif st["broken_count"] >= 3 and not st["is_at_limit"]:
                    sell_reason = f"反复烂板({st['broken_count']}次)"

                # B4: 死亡换手 (来源: 陈小群——换手>60%清仓)
                elif st.get("close", 0) > 0:
                    today_vol = st.get("volume", 0)
                    prev_vol = prev_volume_map.get(sym, 0)
                    vol_ratio = today_vol / prev_vol if prev_vol > 0 else 0
                    if vol_ratio > 0.60:
                        sell_reason = f"死亡换手({vol_ratio:.0%})"

                # B5: 缩量加速 (来源: 陈小群——换手<8%+涨停→次日分批卖)
                if not sell_reason and st["is_at_limit"] and st.get("close", 0) > 0:
                    today_vol = st.get("volume", 0)
                    prev_vol = prev_volume_map.get(sym, 0)
                    vol_ratio = today_vol / prev_vol if prev_vol > 0 else 0
                    if 0 < vol_ratio < 0.08:
                        sell_reason = f"缩量加速({vol_ratio:.0%})"

                if sell_reason:
                    px = st["close"]
                    sell_val = pos["shares"] * px
                    fee = max(sell_val * 0.0003, 5) + sell_val * 0.001
                    pnl = sell_val - pos["shares"] * pos["price"] - fee
                    capital += sell_val - fee
                    record_trade(date.today().isoformat(), sym, "sell", px,
                                pos["shares"], pos.get("board_count", 0),
                                round(pnl, 2), round((px/pos["price"]-1)*100, 2), round(capital, 2))
                    # 释放买入名额
                    tracker.bought.discard(sym)
                    positions.remove(pos)
                    trades_list.append({"symbol": sym, "side": "sell", "price": px,
                                       "shares": pos["shares"], "date": date.today().isoformat(),
                                       "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 1)})
                    logger.info(f"  🔴 盘中卖出 {sym}: {sell_reason} ¥{px:.2f} PnL=¥{pnl:.0f}")

            # 计算总资产 + 持仓明细 (来源: 实时价优先, 未加载时用成本价)
            pos_value = 0
            positions_with_px = []
            for p in positions:
                st = tracker.stocks.get(p["symbol"], {})
                px = st.get("close", 0) if st.get("close", 0) > 0 else p["price"]
                pos_value += p["shares"] * px
                positions_with_px.append({
                    "symbol": p["symbol"], "name": p.get("name", ""),
                    "shares": p["shares"], "price": p["price"],
                    "current": round(px, 2), "board_count": p.get("board_count", 0),
                    "date": p.get("date", ""),
                    "has_sealed": p.get("has_sealed", False),
                    "pnl_pct": round((px / p["price"] - 1) * 100, 2),
                    "value": round(p["shares"] * px, 2),
                })
            total_asset = round(capital + pos_value, 2)

            update_state({"status": "盘中", "progress": "", "capital": round(capital, 2),
                         "total_asset": total_asset, "pos_value": round(pos_value, 2),
                         "positions": positions_with_px,
                         "all_signals": tracker.all_signals,
                         "final_signals": [s for s in new_signals if s['mode'] in ('连板接力','首板试探')],
                         "golden_signals": [s for s in new_signals if s['mode'] in ('弱转强','首阴反包')]})

            # 黄金半小时 3s, 盘中 5s
            if now.hour == 9 and now.minute >= 30 and now.hour < 10:
                time.sleep(3)
            else:
                time.sleep(5)

        conn.close()
        tracker.reset()

        logger.info(f"=== 收盘 | 本金 ¥{capital:,.0f} ===")
        update_state({"status": "已收盘", "capital": round(capital, 2),
                     "summary": f"今日完成, 本金¥{capital:,.0f}"})


if __name__ == "__main__":
    run()
