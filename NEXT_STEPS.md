# NEXT STEPS — 重启 opencode 后从这里继续 (2026-08-15 晚)

> 会话输出通道曾异常, 因此关键状态尽量落盘. 重启后先看: git status / READ_MARKER.

## 1. 后台任务: 行业历史同步 (未完成)

- **进度**: 08-15 停在 **3390/5557** (~61%), 原因 = baostock 当日配额耗尽
  (`BaostockQuotaExceeded`, 每日 50000 次, `quant/utils/baostock_gate.py:140`,
  状态 `quant/data/.baostock_state.json`). **正常保护, 非故障**.
- **断点续跑内置**: `quant/data/industry_history.py:342` 表内已同步符号自动跳过, 重跑不丢.
- **换热点后我试过续跑**: `nohup bash scripts/sync_industry_history.sh > /tmp/industry_sync2.log &`
  — 因同日(08-15)配额按日计数且是本机文件计数, 与出口 IP 无关, 预计仍被立即拒绝.
  **应等跨天(08-16)后再跑**, 或确认热点是否重置了 baostock 服务端配额.
- **进度查看**: `/tmp/industry_sync.log` (第一次, 停前 3390/5557), `/tmp/industry_sync2.log` (续跑尝试),
  `/tmp/pit_activate.log` (激活链轮询).

## 2. 队列任务: PIT 激活链 (等同步完成)

```bash
bash scripts/sync_industry_history.sh   # 跨天后续跑
bash scripts/industry_pit_activate.sh   # 校验覆盖 + smoke 回测 + 重物化 (未完成会 ABORT, 正常)
```

## 3. 已归档完成 (无需重做)

- **v506/v507 已提交并 push**: commit `c4d1fe1` → origin/main (本轮页面前 8 个提交已同步).
- v506 多策略页真实账户数据源 + 系统页 Prometheus/Grafana 显示修复; v507 tab-factors 默认 active 修复.
- `web/app.py VERSION = "test-v507"`. 工作区上轮结束干净.

## 4. 排查残留 (在 /tmp, 无 repo 污染)

- `/tmp/probe_bs.py` 探针, `/tmp/status_snapshot.txt` 状态快照, `/tmp/verify_handoff.txt`.
