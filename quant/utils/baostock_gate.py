"""baostock 全局限速 + 黑名单熔断 — 防 IP 拉黑 (2026-08-14 黑名单事件后新增).

背景: 2026-08-13 全市场回填时被 baostock IP 拉黑 (login error_code 10001011)。
根因: 多个调用点 (nightly 数据同步 / adj_factor 兜底 / 回填脚本) 各自用裸
sleep (0.15s~0.5s) 限速, 进程间互不知晓 — 同 IP 并发请求叠加后频次远超
免费服务容量, 触发封禁。

规则: 所有 baostock 调用点必须经 BaostockGate.acquire() 后再发请求:

1. 跨进程令牌桶: 基于 fcntl 文件锁 + 状态文件 (quant/data/.baostock_state.json),
   scheduler subprocess 链与手动回填脚本共享同一限速, 不再各自 sleep.
2. 黑名单熔断: 调用点检测到黑名单会话错误时调用 mark_blacklisted();
   冷却期内 (baostock_blacklist_cooldown_sec) acquire() 直接抛
   BaostockBlacklisted, 拒绝再发任何请求 — 防止高频重试加重封禁.
3. 配额上限: 每分钟/每日请求数上限 (baostock_calls_per_minute / per_day),
   超出抛 BaostockQuotaExceeded (fail-fast, 不静默降级).

配置 (quant/config/config.yaml data.rate_limit.*):
  baostock_per_stock_sec        最小请求间隔 (秒)
  baostock_calls_per_minute     每分钟请求上限
  baostock_calls_per_day        每日请求上限
  baostock_blacklist_cooldown_sec  黑名单标记后拒绝请求的冷却时长
"""
import json
import random
import time
from pathlib import Path

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("utils.baostock_gate")

_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / ".baostock_state.json"
_LOCK_PATH = _STATE_PATH.with_suffix(".json.lock")

try:
    import fcntl  # POSIX only (darwin/linux 均可用)
except ImportError:  # pragma: no cover — Windows 无 fcntl
    fcntl = None


class BaostockBlacklisted(RuntimeError):
    """IP 已被 baostock 拉黑 (冷却期内) — 立即停止, 勿再请求."""


class BaostockQuotaExceeded(RuntimeError):
    """当日 baostock 请求配额已尽 — 明日再跑."""


class BaostockGate:
    """跨进程令牌桶 — 单例使用, 所有 baostock 调用点共享."""

    def __init__(self):
        self._proc_last_ts = 0.0

    # ── 配置 (热更新, 每次读取) ──
    @property
    def _interval(self) -> float:
        return float(_require_cfg("data.rate_limit.baostock_per_stock_sec", 0.5))

    @property
    def _per_minute(self) -> int:
        return int(_require_cfg("data.rate_limit.baostock_calls_per_minute", 120))

    @property
    def _per_day(self) -> int:
        return int(_require_cfg("data.rate_limit.baostock_calls_per_day", 50000))

    @property
    def _cooldown_sec(self) -> float:
        return float(_require_cfg("data.rate_limit.baostock_blacklist_cooldown_sec", 86400))

    # ── 状态文件 ──
    @staticmethod
    def _load_state() -> dict:
        try:
            return json.loads(_STATE_PATH.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_state(st: dict) -> None:
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(_STATE_PATH)

    def acquire(self) -> None:
        """阻塞直到允许发起下一次 baostock 请求.

        Raises:
            BaostockBlacklisted: IP 在冷却期内 — 停止一切请求.
            BaostockQuotaExceeded: 当日配额已尽 — 明日再跑.
        """
        while True:
            now = time.time()
            # 本进程上次已放行请求间隔已足够 → 尝试更新全局状态
            if now - self._proc_last_ts >= self._jittered_interval():
                try:
                    self._locked_update(now)
                    self._proc_last_ts = time.time()
                    return
                except _NeedWait as _nw:
                    time.sleep(_nw.seconds)
                    continue

            # 本进程间隔不足 → 睡到够 (绝不提前发请求)
            wait = self._jittered_interval() - (now - self._proc_last_ts)
            time.sleep(max(wait, 0.01))

    def _locked_update(self, now: float) -> None:
        """文件锁内: 校验黑名单/配额 + 保证跨进程最小间隔.

        若跨进程间隔不足, 抛 _NeedWait(seconds) — 由 acquire() 释放锁后 sleep.
        """
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("baostock gate requires fcntl (POSIX)")
        f = open(_LOCK_PATH, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            st = self._load_state()
            today = time.strftime("%Y-%m-%d")
            minute = time.strftime("%Y-%m-%d %H:%M")
            interval = self._interval

            # 黑名单冷却期检查 — 拒绝一切请求
            bl_at = st.get("blacklisted_at", 0)
            if bl_at and now - float(bl_at) < self._cooldown_sec:
                remain = int(self._cooldown_sec - (now - float(bl_at)))
                raise BaostockBlacklisted(
                    f"baostock IP 黑名单冷却中 (剩余 {remain}s), 拒绝请求 — "
                    f"标记时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(bl_at))}")

            # 配额窗口滚动
            if st.get("day") != today:
                st = {"day": today, "day_count": 0, "minute": minute, "minute_count": 0}
            if st.get("minute") != minute:
                st["minute"] = minute
                st["minute_count"] = 0
            if st.get("day_count", 0) >= self._per_day:
                raise BaostockQuotaExceeded(
                    f"baostock 当日配额已尽 ({self._per_day}), 明日再跑")
            if st.get("minute_count", 0) >= self._per_minute:
                raise BaostockQuotaExceeded(
                    f"baostock 每分钟配额已尽 ({self._per_minute}), 稍后重试")

            # 跨进程最小间隔
            file_last_ts = float(st.get("last_ts", 0))
            if now - file_last_ts < interval:
                raise _NeedWait(interval - (now - file_last_ts))

            st["day_count"] = st.get("day_count", 0) + 1
            st["minute_count"] = st.get("minute_count", 0) + 1
            st["last_ts"] = now
            self._save_state(st)
        finally:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    def mark_blacklisted(self, error_msg: str = "") -> None:
        """调用点检测到黑名单会话错误时调用 — 记录时间戳, 冷却期内拒绝一切请求."""
        st = self._load_state()
        st["blacklisted_at"] = time.time()
        st["blacklist_msg"] = error_msg
        self._save_state(st)
        logger.error(f"baostock 黑名单标记: {error_msg or 'unknown'} — "
                     f"冷却 {self._cooldown_sec}s 内拒绝一切请求")

    @staticmethod
    def _jittered_interval() -> float:
        """±10% 随机抖动 — 避免多个进程同步请求形成规律节奏."""
        base = float(_require_cfg("data.rate_limit.baostock_per_stock_sec", 0.5))
        return base * random.uniform(0.9, 1.1)


class _NeedWait(Exception):
    """内部信号: 跨进程间隔不足, 需 sleep 后重试."""

    def __init__(self, seconds: float):
        super().__init__(seconds)
        self.seconds = seconds


gate = BaostockGate()


def bs_query(api_name: str, *args, **kwargs):
    """baostock 查询统一入口 — 全局限速 + 黑名单熔断.

    2026-08-14 设计: 所有 baostock 调用 (登录/日线/财务/复权因子) 必须经此,
    替代各点裸 sleep — 跨进程 (nightly + 手动回填) 共享 BaostockGate 令牌桶,
    杜绝同 IP 并发请求叠加触发封禁 (2026-08-13 实测被拉黑 error_code 10001011).
    """
    import baostock as _bs
    gate.acquire()
    rs = getattr(_bs, api_name)(*args, **kwargs)
    if rs.error_code != "0" and "黑名单" in (rs.error_msg or ""):
        gate.mark_blacklisted(f"{api_name}: {rs.error_msg}")
        raise BaostockBlacklisted(f"baostock {api_name}: {rs.error_msg}")
    return rs