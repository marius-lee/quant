# ADR 025: launchd KeepAlive 策略 — scheduler vs webapp

**日期**: 2026-07-07
**状态**: 已生效

## 背景

系统通过 launchd plist 管理两个常驻服务:

| 服务 | plist | 职责 |
|------|-------|------|
| `com.quant.scheduler` | `~/Library/LaunchAgents/com.quant.scheduler.plist` | 三阶段调度 (08:30 / 09:30 / 15:30) |
| `com.quant.webapp` | `~/Library/LaunchAgents/com.quant.webapp.plist` | Flask Web 服务 (端口 8521) |

此前两个 plist 均未设置 `KeepAlive`，也未设置任何重启策略。出现 process 异常退出后无人拉起的空白窗口。

## 决策

**`KeepAlive` 仅加在 scheduler，不加在 webapp。**

| 服务 | KeepAlive | 理由 |
|------|:---:|------|
| scheduler | ✅ | 纯事件循环，代码极少改动。崩溃后应自动恢复，否则错过下一时段直到手动发现。 |
| webapp | ❌ | 代码更新频繁。若 `KeepAlive` 开启，旧进程崩溃后 launchd 直接用旧的 `.pyc` 拉起，新代码不生效 — `restart.sh` 的 `pkill → 清 pycache → bootstrap` 流程被绕过。 |

### webapp 的正确重启流程

```
./restart.sh
  ├─ pkill -f "python.*web/app.py"     # 硬杀旧进程
  ├─ find -name __pycache__ -exec rm   # 清空字节码缓存
  └─ launchctl bootstrap com.quant.webapp  # 用新代码拉起
```

**`KeepAlive` 与此流程冲突**：launchd 在旧进程崩溃后立即拉起，此时：
1. `__pycache__` 未清 → 新 `.py` 文件不生效
2. 用户不知道旧进程已崩 → 界面无异常，代码却是旧版本

### scheduler 为什么例外

scheduler 代码极少改动（上一次更改是加了三阶段逻辑），且它的运行连续性至关重要（错过 09:30 窗口 = 当天无法交易）。`KeepAlive` 确保崩溃后立即恢复，代码版本问题由 `restart.sh` 统一处理（bootout scheduler → 清 pycache → bootstrap）。

## 后果

- 新增 `KeepAlive` 策略必须在 `restart.sh` 和 plist 中同步检查
- 未来若 scheduler 代码频繁改动，应重新评估此决策
- 任何需要手动清缓存的部署流程，不得对目标服务启用 `KeepAlive`
