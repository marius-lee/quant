#!/bin/bash
# 每次提交前必须跑这个脚本。改了什么模块，选对应检查项。
# 用法: ./.pre-commit-check.sh store    # 改了 data/store.py
#       ./.pre-commit-check.sh all       # 全量检查

set -e
MODULE="${1:-all}"

echo "=== Pre-Commit Check: $MODULE ==="

clean_pycache() {
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
    echo "  [OK] pycache cleaned"
}

check_imports() {
    .venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
import ast, os
for f in ['$1']:
    with open(f) as fp:
        ast.parse(fp.read())
    print(f'  [OK] {f} syntax valid')
"
}

check_log_output() {
    # 启动目标模块 5 秒，抓日志，检查关键数值是否符合预期
    local cmd="$1"
    local expect="$2"
    local timeout="${3:-10}"
    echo "  [RUN] $cmd (expect: $expect)"
    timeout $timeout bash -c "$cmd" 2>&1 | grep -q "$expect" && echo "  [OK] log contains '$expect'" || echo "  [FAIL] log missing '$expect'"
}

clean_pycache

case "$MODULE" in
    store|all)
        check_imports "data/store.py"
        # 验证: _analyze_daily_gaps 不再输出 stale 行
        .venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from data.store import DataStore
import inspect
src = inspect.getsource(DataStore._analyze_daily_gaps)
assert 'est_trading < stale_days' not in src, 'BUG: stale_days<250 check still present'
print('  [OK] _analyze_daily_gaps: stale_days check removed')
"
        ;;
    scheduler|all)
        check_imports "scheduler.py"
        ;;
    pipeline|all)
        check_imports "pipeline.py"
        ;;
    web|all)
        check_imports "web/app.py"
        ;;
esac

echo "=== Pre-Commit Check PASSED ==="
