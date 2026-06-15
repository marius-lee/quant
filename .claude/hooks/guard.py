#!/usr/bin/env python3
"""
PreToolUse Hook — 代码守卫

拦截 Write/Edit 到 .py 文件，强制检查"方案已批准"状态。
不依赖 LLM 上下文，不会因会话变长而失效。

协议：
  stdin  → {"tool_name": "Write|Edit", "tool_input": {"file_path": "..."}}
  stdout → {"hookSpecificOutput": {"permissionDecision": "allow|deny", ...}}

批准机制：
  .claude/plan_approved 文件存在且 mtime < 2小时 → 放行
  否则 → 阻止，提示先出方案
"""

import json
import sys
import os
from pathlib import Path

PLAN_FILE = ".claude/plan_approved"
GRACE_HOURS = 2
CODE_EXTENSIONS = {".py", ".css", ".js", ".html", ".yaml", ".yml", ".plist"}


def deny(reason: str) -> None:
    """输出阻止决定并退出"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason
        }
    }))
    sys.exit(0)


def allow() -> None:
    """输出放行决定并退出"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "OK"
        }
    }))
    sys.exit(0)


def is_code_file(file_path: str) -> bool:
    """检查是否为需要守卫的代码文件"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in CODE_EXTENSIONS


def is_plan_approved(project_root: str) -> bool:
    """检查 plan_approved 文件是否存在且未过期"""
    plan_path = os.path.join(project_root, PLAN_FILE)
    if not os.path.exists(plan_path):
        return False

    import time
    mtime = os.path.getmtime(plan_path)
    age_seconds = time.time() - mtime
    return age_seconds < GRACE_HOURS * 3600


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        allow()  # 解析失败不阻塞

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # 只守卫 Write 和 Edit
    if tool_name not in ("Write", "Edit"):
        allow()

    file_path = tool_input.get("file_path", "")

    # 非代码文件放行（允许写 .md 方案文档、配置文件等）
    if not is_code_file(file_path):
        allow()

    # .claude/ 目录内文件放行（允许写 plan_approved 等内部文件）
    if ".claude/" in file_path or file_path.startswith(".claude"):
        allow()

    # 检查方案是否已批准
    cwd = data.get("cwd", os.getcwd())
    if is_plan_approved(cwd):
        allow()

    # 阻止：未批准就写代码
    deny(
        f"🚨 铁律拦截：未经方案批准，禁止编辑代码文件 ({file_path})。\n\n"
        f"请按以下流程操作：\n"
        f"1. 先进行系统性分析（影响范围、可选方案、边界条件）\n"
        f"2. 将方案写入 .md 文件，请总监审阅\n"
        f"3. 总监批准后，执行 `touch {PLAN_FILE}` 解除拦截\n"
        f"4. 然后再写代码\n\n"
        f"⚡ 提示：小改动（修 typo、加日志）如果确实不需要方案，"
        f"可以执行 `touch {PLAN_FILE}` 临时解锁 {GRACE_HOURS} 小时。"
    )


if __name__ == "__main__":
    main()
