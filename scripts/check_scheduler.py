from quant.data.store import DataStore
s = DataStore()
conn = s._connect()

# 1. recent task runs
print("=== 最近任务运行记录 ===")
for r in conn.execute("""
    SELECT task_name, date, status, started_at, finished_at, error
    FROM task_runs ORDER BY started_at DESC LIMIT 15
"""):
    print(f"  {r[0]:20s} | {r[1]} | {r[2]:8s} | {r[3][:19] if r[3] else '':19s} → {r[4][:19] if r[4] else '':19s}")

# 2. scheduled tasks (from scheduler config)
print("\n=== 调度器配置 ===")
import yaml
with open("quant/config/config.yaml") as f:
    cfg = yaml.safe_load(f)
schedule = cfg.get("scheduler", {}).get("tasks", [])
for t in schedule:
    print(f"  {t['name']:20s} @ {t['time']:6s} | {t.get('days','daily'):10s}")

# 3. check if scheduler daemon is running
import subprocess
r = subprocess.run(["pgrep", "-f", "web/app.py"], capture_output=True, text=True)
if r.stdout.strip():
    print(f"\n✅ web/scheduler 进程运行中: PID {r.stdout.strip()}")
else:
    print("\n❌ web/scheduler 进程未运行!")

r2 = subprocess.run(["pgrep", "-f", "daemon.py"], capture_output=True, text=True)
if r2.stdout.strip():
    print(f"✅ scheduler daemon 运行中: PID {r2.stdout.strip()}")
else:
    print("⚠ scheduler daemon 未找到独立进程 (可能内嵌在 web 中)")

s.close()
