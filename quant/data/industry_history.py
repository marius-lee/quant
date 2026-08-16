"""行业分类 PIT 历史同步 — 修正 stocks.industry 当前快照的历史前视 (v502).

背景 (v501 #2 数据缺口闭环):
  因子物化 (_preload) 与回测中性化 (loop/pipeline) 均读 `stocks.industry`
  当前快照 → 历史任意日期拿到的都是"今天"的行业 → 前视污染。
  baostock query_stock_industry(code, date) 返回某日期时点的行业分类:
    - 行业文本按日期真实 PIT (实证: 000009 2020=S91综合 → 今天=J69其他金融业)
    - updateDate 字段为 baostock 周级数据刷新戳 (000001 连续查询每周一跳),
      并非行业真实变更点 → 不能用作变更锚点.

数据源:
  baostock 行业分类 (证监会行业分类)。query_stock_industry 虽支持无 code
  全市场查询, 但服务端翻页失效 (v511 实测: rs.next() 恒 True 且页码不变,
  仅返回首页 500 只) → 会无限循环吃爆内存, 故必须逐股查询.

表结构:
  industry_history(symbol, effective_from, industry)
  主键 (symbol, effective_from) — 每股一条记录 = "自 effective_from 起行业为 X".
  PIT 读取: effective_from <= T 的最大一行.

同步算法 (递归二分变更边界):
  每股在 [start_date, today] 内定位全部行业文本变更边界:
    1. 若 query(start) 与 query(today) 行业相同 → 单段 (start, ind) 完成
    2. 不同 (或左端无记录) → 二分定位最右边界, 递归左段
  复杂度: 不变股每股 2 次查询; 变更股每股 ~2 + log2(天) 次;
  实测全市场约 20% 股有行业变更 → 平均 ~5 次/股.
  session 短 (免费服务 ~2-5 分钟): 每 200 只重登 (同 sync_adj_factor 策略).
  全局限速: 所有 baostock 调用走 BaostockGate (bs_query).

用法:
  PYTHONPATH=. .venv/bin/python -c "from quant.data.industry_history import sync_history; sync_history()"
  bash scripts/sync_industry_history.sh [batch]
"""
import sqlite3
import time as _time
from datetime import date, timedelta

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("data.industry_history")

try:
    from quant.utils.baostock_gate import (
        bs_query as _bs_query, BaostockBlacklisted, BaostockQuotaExceeded, gate as _gate,
    )
except ImportError:  # pragma: no cover
    _bs_query = None
    _gate = None

_RELOGIN_EVERY = 40  # 免费 baostock session 极短 (实测 ~32s/168 次过期), 每 40 次强制重登


def _bs_code(sym: str) -> str:
    """6位数代码 → baostock 格式."""
    if sym.startswith(("6", "9")):
        return f"sh.{sym}"
    return f"sz.{sym}"


def _query_industry(code: str, d: str):
    """查询某股某日行业, 返回 (updateDate, industry) 或 None (无记录)."""
    rs = _bs_query("query_stock_industry", code=code, date=d)
    if rs.error_code != "0":
        raise RuntimeError(f"query_stock_industry {code} {d}: "
                           f"error_code={rs.error_code} msg={rs.error_msg}")
    rows = rs.data or []
    if not rows:
        return None
    row = rows[0]
    return row[0], (row[3] or "").strip()


def _build_table(conn) -> None:
    """industry_history 建表 (幂等)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS industry_history (
            symbol        TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            industry      TEXT NOT NULL,
            PRIMARY KEY (symbol, effective_from)
        );
        CREATE INDEX IF NOT EXISTS idx_industry_history_ef
            ON industry_history(effective_from);
        CREATE TABLE IF NOT EXISTS industry_history_skip (
            symbol     TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)


def _d_add(d_str: str, days: int) -> str:
    return (date.fromisoformat(d_str) + timedelta(days=days)).strftime("%Y-%m-%d")


def _d_sub(d_str: str, days: int) -> str:
    return _d_add(d_str, -days)


class _Prober:
    """带查询计数 + session 重登回调 + 每股结果缓存的行业探测器.

    锚点扫描会重复探测同日期 (缓存复用), 避免重复 query.
    免费 baostock session 极短 (实测 ~32s/168 次即过期): 每 40 次查询强制重登,
    查询遇"未登录"错误自动重登重试一次.
    """

    def __init__(self, on_relogin=None):
        self.queries = 0
        self._on_relogin = on_relogin
        self._cache: dict[str, dict] = {}   # code -> {date: (updateDate, industry) | None}

    def __call__(self, code: str, d: str):
        cc = self._cache.setdefault(code, {})
        if d in cc:
            return cc[d]
        # 每 40 次查询强制重登 (免费 session 极短)
        if self.queries % _RELOGIN_EVERY == 0 and self.queries > 0 and self._on_relogin is not None:
            self._on_relogin()
        try:
            q = _query_industry(code, d)
        except RuntimeError as _re:
            if "未登录" in str(_re) and self._on_relogin is not None:
                self._on_relogin()
                q = _query_industry(code, d)
            else:
                raise
        self.queries += 1
        cc[d] = q
        return q


class _BatchProberUnused:
    """!!! 不可用 — 保留仅作警示（v511 实测推翻）!!!

    baostock `query_stock_industry(code="", date=D)` 服务端翻页失效: rs.next()
    永远 True 且 cur_page_num 恒定不变, 永远返回第一页 500 只 (实测 60 页
    bad=59, 0.6s 无进展) → while rs.next() 无限循环 → 内存爆 → 进程被系统
    kill 无 traceback 静默死亡. 批量全市场快照方案废弃, 恢复逐股探测.
    """

    def __call__(self, code: str, d: str):
        raise RuntimeError("_BatchProber 已废弃 (baostock 翻页 bug), 用 _Prober")


def _anchors(start_date: str, today: str) -> list:
    """半年度锚点日期集 — 行业变更全局批次 (证监会半年度/年度调整).

    含 start_date, 每年 6-30/12-31, today. 锚点分辨率 6 个月, 相邻锚点
    行业不同 → 该区间再二分精化到日.
    """
    res = [start_date]
    y0 = int(start_date[:4])
    y1 = int(today[:4])
    for y in range(y0, y1 + 1):
        res.append(f"{y}-06-30")
        res.append(f"{y}-12-31")
    res.append(today)
    seen, out = set(), []
    for d in res:
        if d not in seen and start_date <= d <= today:
            seen.add(d)
            out.append(d)
    return out


def _find_boundary2(code: str, lo: str, hi: str, prev_ind: str, ind_hi: str, probe,
                    global_days: set = None) -> tuple:
    """在 [lo, hi] 定位变更边界: (last_old_day, first_new_day).

    已知 probe(hi) = ind_hi; 左侧 (lo 或更早) 为 prev_ind (或无记录).
    剪枝: 全局变更日 (global_days) 中落在 (lo, hi] 内的候选 d, 若
      probe(d-1) 为 prev_ind 且 probe(d) = ind_hi → 边界即 d, 免二分 (2 次查询).
    否则回退经典二分 (每步 1 次查询).
    """
    if global_days:
        for _d in sorted(global_days):
            if not (lo < _d <= hi):
                continue
            q_pre = probe(code, _d_sub(_d, 1))
            q_d = probe(code, _d)
            if q_d is not None and q_d[1] == ind_hi \
                    and (q_pre is None or q_pre[1] == prev_ind):
                return _d_sub(_d, 1), _d
    lo_b, hi_b = lo, hi
    while (date.fromisoformat(hi_b) - date.fromisoformat(lo_b)).days > 1:
        mid = (date.fromisoformat(lo_b)
               + (date.fromisoformat(hi_b) - date.fromisoformat(lo_b)) // 2).isoformat()
        q_mid = probe(code, mid)
        if q_mid is not None and q_mid[1] == ind_hi:
            hi_b = mid
        else:
            lo_b = mid
    return lo_b, hi_b


def _symbol_intervals(code: str, start_date: str, today: str,
                      probe, global_days: set = None, list_date: str = None) -> list:
    """两端探测 + 全局变更日剪枝 — 求 [start_date, today] 全部行业段.

    算法 (针对 86% 股票行业不变的事实 — 实测 145 股 avg 1.16 段):
      1. 快速路径: probe(start) vs probe(today) + 中点回归验证 → 3 次查询.
         单段股 (86%) 直接完成.
      2. 变更路径: 半年度锚点扫描定段 → 相邻锚点不同用全局变更日剪枝
         (_find_boundary2 命中免二分).
      3. 次新股 (list_date > start_date, v512): 上市日已知 — 锚点扫描跳过
         上市前区间, 上市日单次确认替代 ~13 次上市日二分 → 每股 ~3-4 次查询
         (原 26-83 次). list_date 无记录时自动回退二分, 正确性不变.
    预期每股查询: 单段 ~3, 变更 ~14+log(段). 全市场 ~2.5万 (实测 9.2万).
    Args:
        global_days: 全局变更日候选集 (变更日多数跨全市场), 命中即免二分.
        list_date:   上市日 (stocks.list_date) — 次新股剪枝用; None 走原路径.
    Returns:
         [(effective_from, industry)] 升序; [] = 全程无记录.
    """
    if global_days is None:
        global_days = set()
    # v512: 次新股 (list_date > start_date) — 用上市日作左端基准: 与 today 相同且
    # 中点回归一致 → 单段 3 次查询 (原 26-83 次). 否则回落锚点路径.
    new_stock = bool(list_date) and list_date > start_date
    q0 = probe(code, list_date) if new_stock else probe(code, start_date)
    q1 = probe(code, today)
    ind0 = q0[1] if q0 else None
    ind1 = q1[1] if q1 else None

    # 上市日探测: 区间内首个有记录日 (左端收窄, 二分 ~log≈13 次钳制在 quartile 内)
    def _first_record(lo: str, hi: str, known: str = None):
        """返回区间内首个有行业记录的日期 (None 语义精细化).

        known: 已知上市日 (次新股) — 单次确认替代二分, 无记录回退二分.
        """
        if known and probe(code, known) is not None:
            return known
        if probe(code, lo) is not None:
            return lo
        c_lo, c_hi = lo, hi
        while (date.fromisoformat(c_hi) - date.fromisoformat(c_lo)).days > 1:
            mid = (date.fromisoformat(c_lo)
                   + (date.fromisoformat(c_hi) - date.fromisoformat(c_lo)) // 2).isoformat()
            if probe(code, mid) is not None:
                c_hi = mid
            else:
                c_lo = mid
        return c_hi if probe(code, c_hi) is not None else None

    # (A) 两端都有记录且行业不同 → 必有变更, 走锚点精化
    # (B) 两端缺失较多 → 走锚点 (上市/退市股)
    # (C) 两端相同 → 单段候选 (中点回归验证)
    if ind0 == ind1 and ind0 is not None:
        mid = (date.fromisoformat(start_date if not new_stock else list_date)
               + (date.fromisoformat(today) - date.fromisoformat(start_date if not new_stock else list_date)) // 2).isoformat()
        qm = probe(code, mid)
        if qm is None or qm[1] == ind0:
            return [(list_date if new_stock else start_date, ind0)]
        # 中点回归 (罕见) → 落入变更路径
    if ind0 is not None and ind1 is not None and ind0 != ind1:
        pass  # 变更, 落入锚点精化
    elif ind0 is None and ind1 is not None:
        pass  # 上市 / 退市后段, 走锚点找第一段
    elif ind0 is not None and ind1 is None:
        pass  # 退市, 走锚点
    elif ind0 is None and ind1 is None:
        return []  # 全程无记录

    # ── 变更路径: 锚点扫描 + 全局变更日剪枝二分 ──
    # 次新股: 锚点区间从上市日对齐的半年期开始, 跳过上市前全 None 段
    eff_start = list_date if (new_stock and list_date) else start_date
    anchors = [a for a in _anchors(eff_start, today) if a >= eff_start]
    snap = []  # [(date, industry|None)]
    for a in anchors:
        q = probe(code, a)
        snap.append((a, q[1] if q else None))

    # 首个记录锚点 (左端无记录 → 二分上市日; 次新股 known=list_date 免二分)
    first_idx = next((i for i, (_, ind) in enumerate(snap) if ind), None)
    if first_idx is None:
        return []  # 全程无记录 (锚点分辨率下)
    fr = snap[first_idx][0]
    if first_idx > 0:
        _fr = _first_record(eff_start, fr, known=list_date if new_stock else None)
        if _fr is not None:
            fr = _fr
    prev_ind = snap[first_idx][1]
    prev_ef = fr
    segments = []
    for i in range(first_idx + 1, len(snap)):
        a, ind = snap[i]
        if ind == prev_ind:
            continue
        if ind is None:
            segments.append((prev_ef, prev_ind))
            prev_ef, prev_ind = a, None
            continue
        lo_b, hi_b = _find_boundary2(code, prev_ef, a, prev_ind, ind, probe,
                                     global_days)
        global_days.add(hi_b)
        segments.append((prev_ef, prev_ind))
        prev_ef, prev_ind = hi_b, ind
    if prev_ind is not None:
        segments.append((prev_ef, prev_ind))

    # 合并相邻同行业段 + 过滤空段
    merged = []
    for ef, ind in segments:
        if not ind:
            continue
        if merged and merged[-1][1] == ind:
            continue
        merged.append((ef, ind))
    return merged


_MAX_INSERT_RETRY = 15      # busy 写锁重试上限 (scheduler 长事务并存)
_INSERT_RETRY_WAIT_S = 8    # 每次重试前等待秒数 → 最多 ~2min

def _insert_with_retry(conn, rows):
    """INSERT OR REPLACE 批量写入 + busy 锁重试 (scheduler 长事务并存场景).

    busy_timeout=120s 仍可能撞 weekly 长事务; 重试至锁释放. 重试用尽仍锁 → 抛错
    (fail-fast, 已提交批次断点续跑不丢).
    """
    for i in range(_MAX_INSERT_RETRY + 1):
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_history "
                "(symbol, effective_from, industry) VALUES (?,?,?)", rows)
            conn.commit()
            return
        except sqlite3.OperationalError as _e:
            if "locked" not in str(_e).lower() or i == _MAX_INSERT_RETRY:
                raise
            _time.sleep(_INSERT_RETRY_WAIT_S)
            _log.info("industry_history: write busy, retry %d/%d", i + 1,
                      _MAX_INSERT_RETRY)


def sync_history(start_date: str = None, today: str = None,
                 symbols: list = None, batch: int = None,
                 conn=None) -> dict:
    """同步全市场行业 PIT 历史 (递归二分变更边界, 幂等续跑).

    Args:
        start_date: 回填起点 (默认 config data.start_date = 2020-01-01)
        today:      回填终点 (默认今天)
        symbols:    指定股票列表 (默认 stocks 表全部)
        batch:      只处理前 batch 只未完成股票 (断点续跑)
        conn:       复用连接 (默认新建)
    Returns:
        {"symbols": n, "points": m, "queries": q, "elapsed": sec}
    """
    from quant.utils.date import to_str
    start_date = start_date or _require_cfg("data.start_date")
    start_date = to_str(start_date)
    today = today or date.today().strftime("%Y-%m-%d")
    if start_date > today:
        raise ValueError(f"start_date {start_date} > today {today}")

    if _bs_query is None:  # pragma: no cover
        raise RuntimeError("baostock not available")

    # v511: 任务级互斥 — 防并行拉取叠加触发 IP 封禁; 并行任务抛 BaostockTaskBusy
    ctx = _gate.task_scope("industry_pit")
    ctx.__enter__()
    try:
        return _sync_history_inner(start_date, today, symbols, batch, conn)
    finally:
        ctx.__exit__(None, None, None)


def _sync_history_inner(start_date: str, today: str, symbols: list, batch: int,
                        conn) -> dict:
    from quant.data.store import DataStore
    own = conn is None
    store = DataStore()
    try:
        conn = conn or store._connect()
        conn.execute("PRAGMA busy_timeout=120000")  # weekly 数据任务长事务, 默认 30s 不够
        _build_table(conn)

        if symbols is None:
            symbols = [r[0] for r in conn.execute(
                "SELECT symbol FROM stocks ORDER BY symbol")]
        if not symbols:
            return {"symbols": 0, "points": 0, "queries": 0, "elapsed": 0.0}

        # 已完成符号 (断点续跑): 表内符号跳过
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM industry_history")}
        # v516: 数据源缺失跳过 (北交所 920 段 baostock 无行业数据; 次新上市初期滞后)
        skip = {r[0] for r in conn.execute(
            "SELECT symbol FROM industry_history_skip")}
        pending = [s for s in symbols if s not in done and s not in skip]

        def _relogin():
            try:
                _bs_query("logout")
            except Exception:
                pass
            lg = _bs_query("login")
            if lg.error_code != "0":
                raise RuntimeError(f"baostock re-login failed: {lg.error_msg}")
            _log.info("industry_history: session re-login")

        probe = _Prober(_relogin)
        # 全局变更日候选集: 从已同步表初始化 + 本次运行累计. 变更日多数跨全市场,
        # 后续每股若在该日变更, _find_boundary2 剪枝命中免二分.
        global_days = {r[0] for r in conn.execute(
            "SELECT DISTINCT effective_from FROM industry_history")}
        total_points = conn.execute(
            "SELECT COUNT(*) FROM industry_history").fetchone()[0]
        lg = _bs_query("login")
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        _log.info("industry_history sync: %d symbols (%d done, %d pending), "
                  "%s → %s", len(symbols), len(done), len(pending),
                  start_date, today)

        t0 = _time.time()
        synced_syms = 0
        n_total = len(pending[:batch] if batch else pending)
        # v512: 次新股剪枝 — 预取上市日, 替代股票上市日二分探测 (每股 -13 次查询)
        list_dates = {r[0]: r[1] for r in conn.execute(
            "SELECT symbol, list_date FROM stocks")}
        try:
            for i, sym in enumerate(pending[:batch] if batch else pending):
                # v513: 日请求上限 — 达到即优雅停止本轮, 提示换热点
                # (baostock 服务端 ~5 万次/日软限制, 2026-08-16 实证被封)
                _dl_reached, _dl_count, _dl_limit = _gate.day_limit_reached()
                if _dl_reached:
                    _log.warning(
                        "baostock 今日请求已达上限 %d/%d — 停止本轮同步. "
                        "换热点(新公网IP)后自动检测续跑 (无需手动重启)",
                        _dl_count, _dl_limit)
                    try:
                        from quant.monitor.notify import send_baostock_quota_alert
                        send_baostock_quota_alert(_dl_count, _dl_limit, len(pending) - i)
                    except Exception:
                        _log.warning("quota alert notify failed", exc_info=True)
                    try:
                        from quant.monitor.alerts import push_baostock_quota_alert
                        push_baostock_quota_alert(_dl_count, _dl_limit, len(pending) - i)
                    except Exception:
                        _log.warning("quota alert push failed", exc_info=True)
                    break
                try:
                    intervals = _symbol_intervals(_bs_code(sym), start_date, today, probe,
                                                  global_days,
                                                  list_date=list_dates.get(sym))
                except (BaostockBlacklisted, BaostockQuotaExceeded) as _ra:
                    raise   # fail-fast: 黑名单/配额不吞
                if not intervals:
                    synced_syms += 1
                    continue
                _insert_with_retry(conn, [(sym, ef, ind) for ef, ind in intervals])
                total_points += len(intervals)
                synced_syms += 1

                # 进度日志 (与 sync_table_full 风格一致): 每 100 只
                if (i + 1) % 100 == 0:
                    el = _time.time() - t0
                    rate = (i + 1) / el if el > 0 else 0
                    eta = (n_total - i - 1) / rate if rate > 0 else 0
                    _log.info("industry_history: %d/%d symbols (+%d points, %d queries), "
                              "%.1fs ETA %.0fs", i + 1, n_total,
                              total_points, probe.queries, el, eta)
        finally:
            try:
                _bs_query("logout")
            except Exception:
                pass
    finally:
        if own:
            store.close()

    elapsed = _time.time() - t0
    _log.info("industry_history done: %d symbols, %d points, %d queries, %.1fs",
              synced_syms, total_points, probe.queries, elapsed)
    return {"symbols": synced_syms, "points": total_points,
            "queries": probe.queries, "elapsed": round(elapsed, 1)}


def industry_for_date(symbols: list, date_str: str,
                      conn=None) -> dict:
    """PIT 查询: 返回 {symbol: industry} — effective_from <= date_str 的最大一行.

    Args:
        symbols: 股票列表
        date_str: 目标日期 (YYYY-MM-DD)
        conn: 复用 SQLite 连接
    Returns:
        dict[str, str] — 无记录股票不在 dict 中
    """
    if not symbols:
        return {}
    from quant.data.store import DataStore
    own = conn is None
    if own:
        store = DataStore()
        conn = store._connect()
    try:
        _build_table(conn)
        ph = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"""
            SELECT h.symbol, h.industry FROM industry_history h
            JOIN (SELECT symbol, MAX(effective_from) AS ef FROM industry_history
                  WHERE symbol IN ({ph}) AND effective_from <= ?
                  GROUP BY symbol) m
              ON m.symbol = h.symbol AND m.ef = h.effective_from
            """,
            tuple(symbols) + (date_str,)).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        if own:
            store.close()