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
from execution.quote import BoardTracker, is_trading_time
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


from web.shared import update_state


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
    update_state({"status": "idle"})

    while True:
        now = datetime.now()

        # 非交易日 → 等明天
        if not is_trading_day(date.today()):
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
            target = now.replace(hour=9, minute=25, second=0)
            wait = (target - now).total_seconds()
            if wait > 0:
                logger.info(f"等待开盘... ({wait:.0f}s)")
                time.sleep(min(wait, 300))  # 最多等5分钟就重检
            continue

        # 盘后 → 等明天
        if now.hour >= 15 and now.minute > 5:
            logger.info("已收盘, 休眠至明早8:00")
            tomorrow = now.replace(hour=8, minute=0, second=0) + timedelta(days=1)
            time.sleep((tomorrow - now).total_seconds())
            continue

        # ═══ 交易时段内: 初始化并监控 ═══
        logger.info(f"=== {date.today()} 开始监控 ===")
        update_state({"status": "init", "progress": "加载行情..."})

        conn = sqlite3.connect(DB)
        watchlist = get_watchlist(conn)
        yesterday = load_yesterday_state(conn)

        # 来源: 一条SQL替代4500条查询 — 昨收价批量加载
        rows = conn.execute("""
            SELECT symbol, close FROM daily
            WHERE date = (SELECT MAX(date) FROM daily WHERE date < DATE('now') AND date LIKE '%-%-%')
        """).fetchall()
        prev_close_map = {r[0]: r[1] for r in rows if r[1]}
        logger.info(f"昨收价加载: {len(prev_close_map)} 只")

        # ── 从 trades.db 恢复状态 (唯一真相源) ──
        capital = 5000.0
        positions = []
        trades_list = []
        try:
            tc = sqlite3.connect(TRADE_DB)
            # 计算本金: 5000 + 所有已卖出交易的PnL总和
            sells = tc.execute("SELECT pnl FROM sim_trades WHERE side='sell'").fetchall()
            capital = 5000.0 + sum(r[0] for r in sells if r[0])
            # 未卖出的持仓
            pos_rows = tc.execute("""
                SELECT symbol, price, shares, board_count, date FROM sim_trades
                WHERE side='buy' AND symbol NOT IN (
                    SELECT symbol FROM sim_trades WHERE side='sell'
                )
            """).fetchall()
            for r in pos_rows:
                # 如果是昨日持仓，今日开盘卖出
                if r[4] < date.today().isoformat():
                    today_open = prev_close_map.get(r[0], r[1])
                    sell_val = r[2] * today_open
                    fee = max(sell_val * 0.0003, 5) + sell_val * 0.001
                    pnl = sell_val - r[2] * r[1] - fee
                    capital += sell_val - fee
                    record_trade(date.today().isoformat(), r[0], "sell", today_open,
                                r[2], r[3], round(pnl, 2),
                                round((today_open/r[1]-1)*100, 2), round(capital, 2))
                    logger.info(f"  卖出昨日持仓 {r[0]}: ¥{r[1]:.2f}→¥{today_open:.2f} PnL=¥{pnl:.0f}")
                else:
                    positions.append({"symbol": r[0], "price": r[1], "shares": r[2],
                                     "board_count": r[3], "date": r[4]})
            # 加载所有历史交易
            all_trades = tc.execute("SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades ORDER BY id").fetchall()
            trades_list = [{"date": t[0], "symbol": t[1], "side": t[2], "price": t[3],
                           "shares": t[4], "pnl": t[5], "pnl_pct": t[6]} for t in all_trades]
            tc.close()
        except Exception:
            pass

        update_state({"status": "init", "progress": "初始化追踪器..."})
        tracker = BoardTracker(yesterday_state=yesterday)
        tracker.start_day(list(prev_close_map.keys()), prev_close_map)
        # 恢复已买入状态 (从今日交易记录中读取)
        try:
            tc2 = sqlite3.connect(TRADE_DB)
            today_buys = tc2.execute(
                "SELECT symbol FROM sim_trades WHERE side='buy' AND date=?",
                (date.today().isoformat(),)
            ).fetchall()
            for (sym,) in today_buys:
                tracker.bought.add((sym, "B1"))
                tracker.bought.add((sym, "B2"))
            tc2.close()
        except Exception:
            pass
        logger.info(f"追踪 {len(tracker.stocks)} 只股票 | 本金 ¥{capital:,.0f} | 已买{len(tracker.bought)}只")

        update_state({"status": "init", "progress": "拉取实时行情...", "capital": round(capital, 2)})

        signal_printed = False

        while is_trading_time():
            now = datetime.now()
            tracker.update()
            new_signals = tracker.scan_all_modes(conn=conn)

            # 新信号 → 即时模拟买入 (来源: ¥5000最多买2只)
            max_pos = 2
            for s in new_signals:
                if len(positions) >= max_pos:
                    break
                sym, mode = s["symbol"], s["mode"]
                if (sym, mode) in tracker.bought:
                    continue
                entry_px = s["price"]
                budget = capital * (0.5 if len(positions) == 0 else 0.3)
                shares = int(budget / entry_px / 100) * 100
                if shares >= 100:
                    cost = shares * entry_px
                    fee = max(cost * 0.0003, 5)
                    if cost + fee < capital:
                        capital -= (cost + fee)
                        positions.append({"symbol": sym, "price": entry_px, "shares": shares,
                                         "date": date.today().isoformat(), "board_count": s.get("board_count", 0)})
                        trades_list.append({"symbol": sym, "side": "buy", "price": entry_px, "shares": shares,
                                           "date": date.today().isoformat()})
                        record_trade(date.today().isoformat(), sym, "buy", entry_px, shares,
                                    s.get("board_count", 0), capital_after=round(capital, 2))
                        tracker.bought.add((sym, mode))
                        logger.info(f"  💰 模拟买入 {sym} ({mode}): ¥{entry_px:.2f} × {shares}股 余额¥{capital:.0f}")

            update_state({"status": "live", "capital": round(capital, 2),
                         "all_signals": tracker.all_signals,
                         "final_signals": [s for s in new_signals if s['mode'] in ('B1_首板试错','B2_二板定龙')],
                         "golden_signals": [s for s in new_signals if s['mode'] in ('B3_首阴反包','B4_分歧转一致')]})

            # 午休: 11:30-13:00 等待
            if now.hour == 11 and now.minute >= 30:
                update_state({"status": "lunch", "capital": round(capital, 2),
                             "all_signals": tracker.all_signals})
                while datetime.now().hour < 13:
                    time.sleep(30)
                logger.info("午休结束, 恢复监控")

            # 黄金半小时 5s, 盘中 30s
            if now.hour == 9 and now.minute >= 30 and now.hour < 10:
                time.sleep(5)
            else:
                time.sleep(30)

            # 14:50 最终信号
            if now.hour == 14 and now.minute >= 50 and not signal_printed:
                from factor.market_mood import detect_mood
                import pandas as pd
                raw = pd.read_sql_query(
                    "SELECT symbol, date, close FROM daily WHERE date >= DATE('now', '-30 days')", conn)
                close_df = raw.pivot(index="date", columns="symbol", values="close") if len(raw) > 0 else pd.DataFrame()
                mood = detect_mood(close_df) if not close_df.empty else {"stage": "复苏", "coefficient": 0.5}

                signals = tracker.get_signals(conn=conn, mood=mood)
                print(f"\n{'='*60}")
                print(f"📊 14:50 收盘买入信号 | 情绪: {mood['stage']}(仓位{mood['coefficient']:.0%})")
                print(f"{'='*60}")
                if signals:
                    for s in signals:
                        leader = "👑龙头" if s.get("is_leader") else ""
                        sectors = ",".join(s.get("sectors", [])[:3])
                        print(f"  {s['symbol']}: ¥{s['price']:.2f} score={s['score']:.3f} "
                              f"board={s['board_count']}连板 mode={s.get('mode','')} {leader}")
                        if sectors:
                            print(f"         板块: {sectors}")
                else:
                    stage = mood.get("stage", "")
                    print(f"  无信号 — 情绪{stage}, 空仓" if stage in ("冰点","退潮") else "  无信号")

                # 自动模拟交易: 信号 → 收盘价买入 (卖出已在今日盘初处理)
                for s in signals:
                    entry_px = s["price"]
                    budget = capital * (mood.get("coefficient", 0.5)) / max(len(signals), 1)
                    shares = int(budget / entry_px / 100) * 100
                    if shares >= 100:
                        cost = shares * entry_px
                        fee = max(cost * 0.0003, 5)
                        capital -= (cost + fee)
                        positions.append({"symbol": s["symbol"], "price": entry_px,
                                         "shares": shares, "date": date.today().isoformat(),
                                         "board_count": s.get("board_count", 0)})
                        record_trade(date.today().isoformat(), s["symbol"], "buy",
                                    entry_px, shares, s.get("board_count", 0),
                                    capital_after=round(capital, 2))
                        trades_list.append({"symbol": s["symbol"], "side": "buy",
                                           "price": entry_px, "shares": shares,
                                           "date": date.today().isoformat()})

                update_state({"status": "signals_ready", "mood": mood,
                             "final_signals": signals, "all_signals": tracker.all_signals,
                             "summary": tracker.get_day_summary(),
                             "capital": round(capital, 2)})
                signal_printed = True

        conn.close()
        tracker.reset()
        logger.info(f"=== 收盘 | 本金 ¥{capital:,.0f} ===")
        update_state({"status": "closed", "capital": round(capital, 2),
                     "summary": f"今日完成, 本金¥{capital:,.0f}"})


if __name__ == "__main__":
    run()
