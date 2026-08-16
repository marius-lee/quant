#!/bin/bash
# scripts/notify_test.sh — 通知通道测试 (v1.0.0, 幂等)
# 用途: 发送一条测试告警走所有已配置通道 (macOS 通知+提示音 / Server酱 / Telegram / 企微),
#       验证手机/桌面可达性. 用于配置 token 后确认通道生效.
# 用法: bash scripts/notify_test.sh [--no-macos] [--title "自定义标题"]
#   --no-macos    跳过 macOS 弹窗/提示音 (只测远程 IM 通道)
#   --title TITLE 自定义测试标题 (默认 "notify 通道测试")
# 幂等性: 只发一条消息, 无副作用; 无配置的通道自动静默跳过.
# 依赖: .venv (requests)

cd "$(dirname "$0")/.." || exit 1

NO_MACOS=0
TITLE="notify 通道测试"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-macos) NO_MACOS=1 ;;
    --title) shift; TITLE="$1" ;;
    *) echo "未知参数: $1 (见脚本头注释)"; exit 1 ;;
  esac
  shift
done

export PYTHONPATH=.

if [ "$NO_MACOS" = "1" ]; then
  .venv/bin/python - <<EOF
from quant.monitor.notify import send_alert
sent = send_alert({"level": "WARNING", "title": "$TITLE", "body": "notify_test.sh 通道连通性验证 — 收到即通道正常."})
print("通道测试完成, 至少一通道送达:" if sent else "所有通道均未送达 (见日志)", sent)
EOF
else
  .venv/bin/python - <<EOF
from quant.monitor.notify import send_alert, _macos_sound
_macos_sound()
sent = send_alert({"level": "WARNING", "title": "$TITLE", "body": "notify_test.sh 通道连通性验证 — 收到即通道正常."})
print("通道测试完成, 至少一通道送达:" if sent else "所有通道均未送达 (见日志)", sent)
EOF
fi
