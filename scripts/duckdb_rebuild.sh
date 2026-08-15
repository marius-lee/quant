#!/bin/bash
set -e
# ── DuckDB 文件收缩: 全量重建 (DROP 后 free blocks 不收缩文件) ──
# v499: 预聚合 8 表 DROP (v498) 后 market.duckdb 2.3G 不缩小 — CHECKPOINT 仅合并
#       WAL、VACUUM 仅重算统计 (实测均无效); 唯一收缩方法 = 拷数据重建文件。
#       拷贝走 DuckDB 内部 INSERT..SELECT (949万+830万行 ~1min), 不落内存。
# 用法: bash scripts/duckdb_rebuild.sh   (自动优雅停机 web/orchestrator)
#       完成后: bash scripts/restart.sh  重新拉起
# 幂等: 以 market.duckdb.new 中间文件重建, 行数校验通过才原子替换;
#       失败则删除 .new, 旧库分毫未动, 可重跑; 旧库留 market.duckdb.bak 备份
cd "$(dirname "$0")/.."

# ── 0) 优雅停机 (同 restart.sh, 确保无进程持锁, mv 才安全) ──
_PATS=("from quant.scheduler import start_all" "from quant.scheduler.orchestrator import start")
for pat in "${_PATS[@]}"; do
  pkill -TERM -f "$pat" 2>/dev/null || true
done
lsof -ti:8521 | xargs kill -TERM 2>/dev/null || true
sleep 5
for pat in "${_PATS[@]}"; do
  pkill -KILL -f "$pat" 2>/dev/null || true
done
lsof -ti:8521 | xargs kill -KILL 2>/dev/null || true
sleep 1

PYTHONPATH=. .venv/bin/python - <<'EOF'
import os, time, duckdb
import quant.data.duckdb_store as ds

OLD = str(ds._DUCKDB_PATH)
NEW = OLD + ".new"

# 0) 清理残留中间文件 (幂等)
if os.path.exists(NEW):
    os.remove(NEW)

# 1) 建新库: 13 张表 DDL (保留主键 — _upsert_df ON CONFLICT 依赖) + 拷贝
conn = duckdb.connect(OLD)
try:
    conn.execute(f"ATTACH '{NEW}' AS nb (TYPE duckdb)")
    for name, ddl in sorted(ds._TABLE_SCHEMAS.items()):
        conn.execute(ddl.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE IF NOT EXISTS nb.", 1))
    t_all = time.time()
    for name in sorted(ds._TABLE_SCHEMAS):
        t0 = time.time()
        conn.execute(f"INSERT INTO nb.{name} SELECT * FROM {name}")
        print(f"  copy {name}: {time.time()-t0:.1f}s")
    print(f"copy done: {time.time()-t_all:.1f}s total")

    # 2) 行数校验 — 不等即崩 (零 fallback), 旧库未动
    for name in sorted(ds._TABLE_SCHEMAS):
        old_c = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        new_c = conn.execute(f"SELECT COUNT(*) FROM nb.{name}").fetchone()[0]
        assert old_c == new_c, f"{name}: old={old_c} new={new_c}"
    print("verify: all 13 tables match")
finally:
    conn.close()

# 3) 原子替换 (此时已无任何连接)
os.rename(OLD, OLD + ".bak")
os.rename(NEW, OLD)

# 4) 新库走 manager 初始化 → 自动补建全部辅助索引 (_ensure_schema) + 复验
m = ds.get_duckdb_proxy()._duckdb
total = sum(m.query_df(f"SELECT COUNT(*) AS c FROM {name}")['c'][0]
            for name in sorted(ds._TABLE_SCHEMAS))
m.close()
old_gb = os.path.getsize(OLD + ".bak") / 2**30
new_gb = os.path.getsize(OLD) / 2**30
print(f"shrunk: {old_gb:.2f} GB -> {new_gb:.2f} GB (freed {old_gb-new_gb:.2f} GB), rows={total:,}")
EOF

# ── 5) 收尾: 保留 .bak 供回滚 (确认无恙后可 rm), 提示重启 ──
echo "done. 备份留存: quant/data/market.duckdb.bak (确认无误后 rm 释放磁盘)"
echo "重启服务: bash scripts/restart.sh"
exit 0
