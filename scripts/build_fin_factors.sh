#!/bin/bash
# 基于 JQData 财务报表构建新因子：ROE, ROA, Debt, Accruals
# 步骤: 1) 加入 factor_registry　2) 写 compute 函数　3) 评估 IC
set -e
cd "$(dirname "$0")/.."

echo "=== Step 1: 注册新因子到 factor_registry ==="
.venv/bin/python3 << 'PYEOF'
import sqlite3, os
db = os.path.join("data", "market.db")
conn = sqlite3.connect(db)

factors = [
    ("roe_reported",  "profitability", "compute_roe_reported",  "Fama & French (2015) — ROE from reported financials"),
    ("roa",           "profitability", "compute_roa",           "Novy-Marx (2013) — gross profitability"),
    ("debt_ratio",    "leverage",      "compute_debt_ratio",    "Penman et al. (2007) — leverage"),
    ("accruals",      "quality",       "compute_accruals",      "Sloan (1996) — earnings quality / accruals anomaly"),
]

for name, cat, fn, src in factors:
    conn.execute("""
        INSERT OR IGNORE INTO factor_registry (name, category, compute_fn, academic_source, status, direction, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'inactive', 'positive', datetime('now','localtime'), datetime('now','localtime'))
    """, (name, cat, fn, src))

conn.commit()
conn.close()
print(f"Registered {len(factors)} new factors")
PYEOF

echo ""
echo "=== Step 2: 更新 factor/compute.py 加入计算函数 ==="
.venv/bin/python3 << 'PYEOF'
import os

# Read current file
path = os.path.join("factor", "compute.py")
content = open(path).read()

# Check idempotency: if all 4 functions already exist, skip
if 'def compute_roe_reported' in content and 'def compute_roa' in content and 'def compute_debt_ratio' in content and 'def compute_accruals' in content:
    print("All 4 factor functions already exist, skipping Step 2")
    exit(0)

# Add new factor compute functions after the existing fundamental factors

new_code = '''
def compute_roe_reported(fundamentals, date):
    """报告期 ROE = net_profit / total_owner_equities
    来源: Fama & French (2015) — 盈利能力因子
    """
    from data.store import DataStore
    store = DataStore()
    fin = store.get_financials(fundamentals.index.tolist(), date=date)
    store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_owner_equities" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roe_reported")
    roe = fin["net_profit"] / fin["total_owner_equities"]
    roe = roe.replace([np.inf, -np.inf], np.nan)
    roe = roe.where((roe > -1) & (roe < 1))  # filter extreme
    return _cs_zscore(roe.reindex(fundamentals.index)).rename("roe_reported")


def compute_roa(fundamentals, date):
    """ROA = net_profit / total_assets
    来源: Novy-Marx (2013) — 盈利能力
    """
    from data.store import DataStore
    store = DataStore()
    fin = store.get_financials(fundamentals.index.tolist(), date=date)
    store.close()
    if fin.empty or "net_profit" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="roa")
    roa = fin["net_profit"] / fin["total_assets"]
    roa = roa.replace([np.inf, -np.inf], np.nan)
    roa = roa.where((roa > -0.5) & (roa < 0.5))
    return _cs_zscore(roa.reindex(fundamentals.index)).rename("roa")


def compute_debt_ratio(fundamentals, date):
    """资产负债率 = total_liability / total_assets（低分=低负债=好）
    来源: Penman et al. (2007)
    """
    from data.store import DataStore
    store = DataStore()
    fin = store.get_financials(fundamentals.index.tolist(), date=date)
    store.close()
    if fin.empty or "total_liability" not in fin.columns or "total_assets" not in fin.columns:
        return pd.Series(np.nan, index=fundamentals.index, name="debt_ratio")
    dr = fin["total_liability"] / fin["total_assets"]
    dr = dr.replace([np.inf, -np.inf], np.nan)
    dr = dr.where((dr > 0) & (dr < 2))
    # 低负债=高分 (取负号), IC=可能正向(高负债在A股可能预示扩张)
    return _cs_zscore(dr).rename("debt_ratio")


def compute_accruals(fundamentals, date):
    """应计利润 = (net_profit - net_operate_cash_flow) / total_assets
    来源: Sloan (1996) — 低应计利润=高质量盈利=未来高收益
    取负号: 低应计→高分
    """
    from data.store import DataStore
    store = DataStore()
    fin = store.get_financials(fundamentals.index.tolist(), date=date)
    store.close()
    needed = ["net_profit", "net_operate_cash_flow", "total_assets"]
    if fin.empty or not all(c in fin.columns for c in needed):
        return pd.Series(np.nan, index=fundamentals.index, name="accruals")
    acc = (fin["net_profit"] - fin["net_operate_cash_flow"]) / fin["total_assets"]
    acc = acc.replace([np.inf, -np.inf], np.nan)
    acc = acc.where((acc > -1) & (acc < 1))
    # 低应计→高分 (IC=负向)
    return _cs_zscore(-acc).rename("accruals")

# 注册到 _FUNDAMENTAL_FN_MAP
if "roe_reported" not in _FUNDAMENTAL_FN_MAP:
    _FUNDAMENTAL_FN_MAP["roe_reported"] = ("profitability", compute_roe_reported)
if "roa" not in _FUNDAMENTAL_FN_MAP:
    _FUNDAMENTAL_FN_MAP["roa"] = ("profitability", compute_roa)
if "debt_ratio" not in _FUNDAMENTAL_FN_MAP:
    _FUNDAMENTAL_FN_MAP["debt_ratio"] = ("leverage", compute_debt_ratio)
if "accruals" not in _FUNDAMENTAL_FN_MAP:
    _FUNDAMENTAL_FN_MAP["accruals"] = ("quality", compute_accruals)
'''

# Insert before the line that defines _FUNDAMENTAL_FN_MAP (or after it)
# Actually, let me insert after the existing fundamental factors (compute_analyst_buy)
insert_marker = "def get_factor_names() -> list:"
if insert_marker not in content:
    print(f"ERROR: marker '{insert_marker}' not found")
    exit(1)

content = content.replace(insert_marker, new_code + "\n\n" + insert_marker)
open(path, "w").write(content)
print("Added 4 new factor compute functions + FUNDAMENTAL_FN_MAP entries")
PYEOF

echo ""
echo "=== Step 3: 添加 get_financials() 到 store.py ==="
.venv/bin/python3 << 'PYEOF'
import os

path = os.path.join("data", "store.py")
content = open(path).read()

# Check idempotency
if 'def get_financials' in content:
    print("get_financials() already exists, skipping Step 3")
    exit(0)

new_method = '''
    def get_financials(self, symbols: list, date: str = None) -> "pd.DataFrame":
        """读取最近季度的财务报表数据(合并三表)。

        symbols: 股票代码列表
        date: 交易日期 → 取最近 stat_date <= date 的季度数据
        返回: DataFrame(index=symbol, columns=[net_profit, total_assets, ...])
        """
        import pandas as pd

        conn = self._connect()
        if not date:
            date = datetime.today().strftime("%Y-%m-%d")

        # 对每只股票取最新季度数据(三表 JOIN)
        placeholders = ",".join("?" * len(symbols))

        # 找到每个symbol在每个表中最新的 stat_date
        df = pd.DataFrame()

        for tbl in ["balance", "income", "cash_flow"]:
            try:
                sub = pd.read_sql_query(f"""
                    SELECT f.symbol, f.stat_date, f.* FROM financial_{tbl} f
                    INNER JOIN (
                        SELECT symbol, MAX(stat_date) as max_sd
                        FROM financial_{tbl}
                        WHERE stat_date <= ? AND symbol IN ({placeholders})
                        GROUP BY symbol
                    ) latest ON f.symbol = latest.symbol AND f.stat_date = latest.max_sd
                """, conn, params=[date] + symbols)
                if not sub.empty:
                    sub = sub.set_index("symbol")
                    # drop duplicate columns
                    sub = sub.loc[:, ~sub.columns.duplicated()]
                    if df.empty:
                        df = sub
                    else:
                        # merge on index
                        common = [c for c in sub.columns if c not in df.columns or c in ["stat_date"]]
                        df = df.join(sub[common], how="outer", rsuffix="_dup")
            except Exception as e:
                pass  # table might not exist yet

        # Clean up: deduplicate stat_date column
        if "stat_date" in df.columns:
            # keep first non-null
            pass

        return df

'''

# Insert before get_fundamentals or before if __name__
marker = "    def get_fundamentals(self, symbols: list = None, date: str = None) -> pd.DataFrame:"
if marker not in content:
    print(f"ERROR: marker not found")
    exit(1)

content = content.replace(marker, new_method + "\n" + marker)
open(path, "w").write(content)
print("Added get_financials() to DataStore")
PYEOF

echo ""
echo "=== Done ==="
echo "Next: bash scripts/eval_new_factors.sh"
