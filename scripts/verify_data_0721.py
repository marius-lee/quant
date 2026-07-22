from quant.data.store import DataStore
s = DataStore()
conn = s._connect()

# 1. basic stats
row = conn.execute("""
    SELECT COUNT(*), COUNT(CASE WHEN turnover>0 THEN 1 END),
           MIN(close), MAX(close),
           MIN(volume), MAX(volume),
           MIN(amount), MAX(amount)
    FROM daily WHERE date='2026-07-21'
""").fetchone()
print(f"07-21: total={row[0]}  turnover>0={row[1]}")
print(f"  close: {row[2]:.2f} ~ {row[3]:.2f}")
print(f"  volume: {row[4]:.0f} ~ {row[5]:.0f}")
print(f"  amount(千元): {row[6]:.1f} ~ {row[7]:.1f}")

# 2. anomalies: zero/negative OHLC
bad = conn.execute("""
    SELECT COUNT(*) FROM daily WHERE date='2026-07-21'
    AND (open<=0 OR high<=0 OR low<=0 OR close<=0)
""").fetchone()[0]
print(f"  bad OHLC (<=0): {bad}")

# 3. vs 07-20: big price jumps (>20%)
jumps = conn.execute("""
    SELECT COUNT(*) FROM daily a JOIN daily b
    ON a.symbol=b.symbol AND b.date='2026-07-20'
    WHERE a.date='2026-07-21'
    AND ABS(a.close/b.close - 1) > 0.2
""").fetchone()[0]
print(f"  close jump >20% vs 07-20: {jumps}")

# 4. turnover distribution
t = conn.execute("""
    SELECT
        CASE WHEN turnover=0 THEN '0'
             WHEN turnover<0.01 THEN '<1%'
             WHEN turnover<0.05 THEN '1-5%'
             WHEN turnover<0.10 THEN '5-10%'
             ELSE '>10%' END AS bucket,
        COUNT(*)
    FROM daily WHERE date='2026-07-21'
    GROUP BY bucket ORDER BY
        CASE bucket WHEN '0' THEN 1 WHEN '<1%' THEN 2
        WHEN '1-5%' THEN 3 WHEN '5-10%' THEN 4 ELSE 5 END
""").fetchall()
print("\n  turnover distribution:")
for b, c in t:
    print(f"    {b}: {c}")

# 5. top/bottom movers
print("\n  top gainers (>9%):")
for r in conn.execute("""
    SELECT symbol, open, close, ROUND((close-open)/open*100,1) AS pct
    FROM daily WHERE date='2026-07-21' AND open>0
    ORDER BY (close-open)/open DESC LIMIT 5
"""):
    print(f"    {r[0]}: {r[1]:.2f}→{r[2]:.2f} ({r[3]:+.1f}%)")

print("\n  top losers (<-9%):")
for r in conn.execute("""
    SELECT symbol, open, close, ROUND((close-open)/open*100,1) AS pct
    FROM daily WHERE date='2026-07-21' AND open>0
    ORDER BY (close-open)/open ASC LIMIT 5
"""):
    print(f"    {r[0]}: {r[1]:.2f}→{r[2]:.2f} ({r[3]:+.1f}%)")

s.close()
