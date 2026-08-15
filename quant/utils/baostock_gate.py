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


# v494: baostock 客户端 socketutil.send_msg 硬编码 UTF-8 解码响应, 服务端
# 返回 GBK 编码的中文错误消息 (如 "接收数据异常") 时抛 UnicodeDecodeError,
# 被库内部吞掉 → 返回 None → 调用方拿到 None 再 AttributeError.
# 一次性修补: 模块 import 即打补丁 (UTF-8 失败回退 GB18030), 全进程生效
# (store.py 裸 import baostock 的路径同样覆盖).
_PATCHED_BAOSTOCK = False


def _patch_baostock_gbk() -> None:
    """monkey-patch baostock.util.socketutil.send_msg — GBK 回退解码.

    幂等 (全局标志); 库缺失/结构变化时静默跳过 (不影响非 baostock 路径).
    """
    global _PATCHED_BAOSTOCK
    if _PATCHED_BAOSTOCK:
        return
    try:
        from baostock.util import socketutil as _su
    except Exception:
        return
    src = getattr(_su, "send_msg", None)
    if src is None:
        return

    import inspect
    try:
        text = inspect.getsource(src)
    except (OSError, TypeError):
        return

    # 定位 'bytes.decode(receive)' 调用 (两处: 压缩/非压缩分支)
    if "bytes.decode(receive)" not in text:
        return

    def _send_msg_gbk(msg):
        """send_msg 的 GBK 兼容版 — UTF-8 失败回退 GB18030 (v494)."""
        import baostock.common.contants as _c
        import baostock.common.context as _ctx
        import socket as _sock
        import zlib as _zlib
        try:
            default_socket = getattr(_ctx, "default_socket", None)
            if default_socket is None:
                return None
            msg = msg + "\n"
            default_socket.send(bytes(msg, encoding='utf-8'))
            receive = b""
            while True:
                recv = default_socket.recv(8192)
                receive += recv
                if receive[-13:] == b"<![CDATA[]]>\n":
                    break
            head_bytes = receive[0:_c.MESSAGE_HEADER_LENGTH]
            head_str = bytes.decode(head_bytes)
            head_arr = head_str.split(_c.MESSAGE_SPLIT)
            if head_arr[1] in _c.COMPRESSED_MESSAGE_TYPE_TUPLE:
                head_inner_length = int(head_arr[2])
                body = _zlib.decompress(
                    receive[_c.MESSAGE_HEADER_LENGTH:_c.MESSAGE_HEADER_LENGTH + head_inner_length])
                return head_str + _decode_gbk(body)
            return _decode_gbk(receive)
        except Exception as ex:
            from quant.utils.logger import get_logger
            get_logger("utils.baostock_gate").warning(f"baostock send_msg failed: {ex}")
            return None

    def _decode_gbk(raw: bytes) -> str:
        try:
            return bytes.decode(raw)
        except UnicodeDecodeError:
            return bytes.decode(raw, "gb18030", errors="replace")

    _su.send_msg = _send_msg_gbk
    _PATCHED_BAOSTOCK = True
    from quant.utils.logger import get_logger
    get_logger("utils.baostock_gate").info("baostock GBK 解码补丁已生效 (UTF-8→GB18030 回退)")


# import 即打补丁 — 任何调用方 (store.py 裸 import baostock / 脚本) 都覆盖
_patch_baostock_gbk()


def bs_query(api_name: str, *args, **kwargs):
    """baostock 查询统一入口 — 全局限速 + 黑名单熔断 + GBK 解码补丁.

    2026-08-14 设计: 所有 baostock 调用 (登录/日线/财务/复权因子) 必须经此,
    替代各点裸 sleep — 跨进程 (nightly + 手动回填) 共享 BaostockGate 令牌桶,
    杜绝同 IP 并发请求叠加触发封禁 (2026-08-13 实测被拉黑 error_code 10001011).
    """
    _patch_baostock_gbk()
    import baostock as _bs
    gate.acquire()
    rs = getattr(_bs, api_name)(*args, **kwargs)
    if rs.error_code != "0" and "黑名单" in (rs.error_msg or ""):
        gate.mark_blacklisted(f"{api_name}: {rs.error_msg}")
        raise BaostockBlacklisted(f"baostock {api_name}: {rs.error_msg}")
    return rs