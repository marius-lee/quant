"""基本面数据同步。

数据源:
  1. 腾讯财经 qt.gtimg.cn — 批量 PE/PB/市值, 60只/次
  2. 东方财富 push2 — 备用 (TODO: 尚未实现，当前仅使用腾讯财经)

TODO: 实现东方财富回退路径，提高数据获取鲁棒性
"""

import re
import sqlite3
import time
import urllib.request

from utils.logger import get_logger

logger = get_logger("data.fundamental")


def sync_all(conn, max_pb_fetch: int = 0) -> dict:
    """全量同步 PE/PB/市值。conn 为 DataStore 共享连接。"""
    _ensure_columns(conn)

    result = _tencent_batch(conn)

    mv_count = conn.execute(
        "SELECT COUNT(*) FROM stocks WHERE total_mv > 0"
    ).fetchone()[0]

    logger.info(
        f"fundamentals done: PE={result['pe_count']} "
        f"PB={result['pb_count']} MV={mv_count}"
    )
    return result


def _ensure_columns(conn):
    for col, typ in _FUND_COLS:
        try:
            conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass


# ── 腾讯财经批量接口 ──
# 字段映射 (已验证):
#   [52]=PE(动态) [53]=PE(TTM) [46]=PB [44]=总市值(亿) [45]=流通市值(亿)
#   [56]=股息率(%) [62]=每股收益 [63]=每股净资产 [64]=每股现金流
#   [67]=52周最高 [68]=52周最低 [39]=换手率(%)

_FIELD_MAP = {
    "pe": 52, "pe_ttm": 53, "pb": 46, "total_mv": 44, "circ_mv": 45,
    "div_yield": 56, "eps": 62, "bvps": 63, "cfps": 64,
    "high_52w": 67, "low_52w": 68, "turnover_rate": 39,
}

# 需要确保存在的 stocks 表列
_FUND_COLS = [
    ("pe","REAL"),("pe_ttm","REAL"),("pb","REAL"),
    ("total_mv","REAL"),("circ_mv","REAL"),
    ("div_yield","REAL"),("eps","REAL"),("bvps","REAL"),("cfps","REAL"),
    ("high_52w","REAL"),("low_52w","REAL"),("turnover_rate","REAL"),
]


def _tencent_batch(conn) -> dict:
    """腾讯财经批量 PE/PB/市值/股息/每股指标, 66只/请求"""
    batch_size = 60
    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks ORDER BY symbol"
    ).fetchall()]

    updated = 0
    total_fails = 0
    max_retries, consecutive_fails = 3, 0

    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i + batch_size]
        codes = []
        for s in chunk:
            if s.startswith(("4","8","92")):   codes.append(f"bj{s}")  # 北交所
            elif s.startswith(("6","9","68")): codes.append(f"sh{s}")  # 上交所
            else:                              codes.append(f"sz{s}")  # 深交所
        url = f"http://qt.gtimg.cn/q={','.join(codes)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            raw = resp.read().decode("gbk", errors="replace")

            for line in raw.strip().split("\n"):
                match = re.search(r'v_(\w+)="([^"]*)"', line)
                if not match: continue
                code = match.group(1)
                sym = code[2:] if code.startswith(("sh","sz","bj")) else code
                fields = match.group(2).split("~")
                if len(fields) < 69: continue

                # 提取所有基本面字段
                vals = {}
                for name, idx in _FIELD_MAP.items():
                    raw_val = fields[idx] if idx < len(fields) else ""
                    if raw_val and _is_number(raw_val):
                        vals[name] = float(raw_val)

                if vals:
                    # 市值从亿转为亿(原值), PB/PE保持原值
                    if "total_mv" in vals: pass  # 已是亿
                    set_clauses = ", ".join(f"{k}=?" for k in vals)
                    conn.execute(
                        f"UPDATE stocks SET {set_clauses} WHERE symbol=?",
                        list(vals.values()) + [sym]
                    )
                    updated += 1

            consecutive_fails = 0
            time.sleep(0.15)

        except Exception as e:
            consecutive_fails += 1
            total_fails += 1
            total_batches = (len(symbols) + batch_size - 1) // batch_size
            logger.warning(f"tencent batch {i}: {e} (fail {consecutive_fails}/{max_retries})")
            if total_fails > total_batches * 0.2:
                logger.error(f"tencent: >20% batches failed ({total_fails}/{total_batches}), stopping")
                break
            if consecutive_fails >= max_retries:
                logger.error("tencent: max consecutive retries, stopping")
                break
            time.sleep(3)

        if (i // batch_size + 1) % 15 == 0:  # 每15批(900只)提交一次
            conn.commit()
            logger.info(f"tencent: {min(i+batch_size, len(symbols))}/{len(symbols)}, {updated} updated")

    conn.commit()
    return {"pe_count": updated, "pb_count": updated}


def _is_number(s: str) -> bool:
    if not s or s == "-": return False
    try: float(s); return True
    except ValueError: return False
