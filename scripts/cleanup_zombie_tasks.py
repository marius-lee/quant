from quant.data.store import DataStore

s = DataStore()
conn = s._connect()

# 清理 07-21 的僵尸 task_runs
conn.execute("""UPDATE task_runs SET status='cleaned', finished_at=datetime('now'),
    error='manual cleanup 07-22: data complete via manual runs'
    WHERE date='2026-07-21' AND status IN ('running','aborted')""")
conn.commit()
print(f"cleaned {conn.total_changes} zombie task_runs for 07-21")

# 验证 07-22 今天干净
rows = conn.execute("SELECT task_name, status, date FROM task_runs WHERE date='2026-07-22'").fetchall()
print(f"07-22 tasks: {len(rows)}")
for r in rows:
    print(f"  {r[0]}: {r[1]}")

s.close()
