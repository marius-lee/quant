"""Reset all 'rejected' factors → 'backtesting' so they can be re-evaluated.

Run once after backtest pipeline bug fixes. 不会改 active/retired 因子。
"""
import sqlite3, sys, os

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB  = os.path.join(PROJ, "quant", "data", "market.db")

def main():
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT name, status FROM factor_registry WHERE status='rejected'").fetchall()
    if not rows:
        print("No rejected factors found.")
        db.close()
        return
    print(f"Resetting {len(rows)} rejected factors → backtesting:")
    for name, _ in rows:
        print(f"  {name}")
    db.execute(
        "UPDATE factor_registry SET status='backtesting', status_reason='reset for retest', "
        "updated_at=datetime('now','localtime') WHERE status='rejected'"
    )
    db.commit()
    print(f"\nDone. {db.total_changes} rows updated.")
    db.close()

if __name__ == "__main__":
    main()
