#!/usr/bin/env bash
# Grafana 仪表盘导出到 provisioning 目录 (v508) — 一键生成 quant 3 张仪表盘 JSON.
#
# 用法: bash scripts/export_grafana_dashboards.sh
# 幂等: 覆盖生成 3 个 JSON 到 /opt/homebrew/etc/grafana/provisioning/dashboards/,
#       下次 Grafana 重启或自动 reload 时加载 (无需手动导入).
# 依赖: .venv (prometheus_client)
set -euo pipefail
cd "$(dirname "$0")/.."

DEST=${1:-/opt/homebrew/etc/grafana/provisioning/dashboards}
mkdir -p "$DEST"

PYTHONPATH=. .venv/bin/python - "$DEST" <<'EOF'
import json, sys
from quant.monitoring.prometheus import GrafanaDashboardBuilder

dest = sys.argv[1]
dashboards = GrafanaDashboardBuilder.export_all(dest)
print(f"exported {len(dashboards)} dashboards -> {dest}")
for name in dashboards:
    print("  ", name, "uid=", dashboards[name]["uid"])
EOF
echo "完成 — Grafana 将自动加载 (或等待 reload)"