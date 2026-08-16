"""baostock 全局限速 + 黑名单熔断 — 防 IP 拉黑 (2026-08-14 黑名单事件后新增).

背景: 2026-08-13 全市场回填时被 baostock IP 拉黑 (login error_code 10001011)。
根因: 多个调用点 (nightly 数据同步 / adj_factor 兜底 / 回填脚本) 各自用裸
sleep (0.15s~0.5s) 限速, 进程间互不知晓 — 同 IP 并发请求叠加后频次远超
免费服务容量, 触发封禁。

防封机制 (v511 取消硬配额后, v513 修正):
1. 跨进程令牌桶: 基于 fcntl 文件锁 + 状态文件 (quant/data/.baostock_state.json),
   scheduler subprocess 链与手动回填脚本共享同一限速, 不再各自 sleep —
   并行任务请求被强制串行化 (最小间隔 0.5s/请求), 这是防封的核心.
2. 黑名单熔断: 调用点检测到黑名单会话错误时调用 mark_blacklisted();
   冷却期内 (baostock_blacklist_cooldown_sec) acquire() 直接抛
   BaostockBlacklisted, 拒绝再发任何请求 — 防止高频重试加重封禁.
3. 每分钟请求上限 (baostock_calls_per_minute, 与 0.5s 间隔等价的双保险).

v511 删除每日请求配额 (baostock_calls_per_day) — 当时判断 baostock 无官方
日配额。**该结论被 2026-08-16 实证推翻**: day_count=52956 时服务端直接拉黑
("黑名单用户，请与管理员联系")。v513 恢复日上限但改为**软上限**:
  - 不做硬拦截 (不抛错), 由 task_scope 内的长任务循环检查 day_limit_reached()
    优雅停止 + 提示换热点;
  - 换热点 (公网 IP 变化) 自动检测: 任务开始时探测 IP, 与 last_ip 不同 →
    自动清零日计数 + 解除黑名单冷却 (新 IP 不承继旧 IP 封禁), 无需人工干预;
  - 保留手动脚本 scripts/reset_baostock_day.sh 作兜底.

配置 (quant/config/config.yaml data.rate_limit.*):
  baostock_per_stock_sec        最小请求间隔 (秒)
  baostock_calls_per_minute     每分钟请求上限
  baostock_calls_per_day        日请求软上限 (达上限优雅停止 + 提示换热点)
  baostock_ip_probe_url         公网 IP 探测 URL (换热点检测)
  baostock_blacklist_cooldown_sec  黑名单标记后拒绝请求的冷却时长
"""
import contextlib
import functools
import json
import os
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
    """兼容保留 (v511 起不再抛出) — 历史上为日配额/分钟配额错误."""


class BaostockTaskBusy(RuntimeError):
    """另一 baostock 拉取任务正在运行 (进程级互斥) — 本任务拒绝启动.

    v511: 防驱动数据源封禁的关键 — 并行任务即使各自限速也会叠加请求;
    任务级互斥保证同一时刻只有一个拉取任务在跑 (网络请求经 gate 再串行),
    从源头杜绝并发. 零 fallback: 调用点必须处理, 不得静默跳过.
    """


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

            # v511: 每日配额已取消 — baostock 无官方日配额, 防封只靠频率
            # (间隔 + 每分钟上限)。day_count 仅作统计留存, 不再拦截请求.
            # 窗口滚动: day 滚动仅刷新统计, minute 滚动刷新限流窗口
            if st.get("day") != today:
                st = {"day": today, "day_count": 0, "minute": minute, "minute_count": 0}
            if st.get("minute") != minute:
                st["minute"] = minute
                st["minute_count"] = 0
            # 每分钟配额上限 (与 0.5s 间隔等价的双保险, 防 min 窗口突发)
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

    # ── 日请求软上限 + 换热点自动检测 (v513) ──
    # baostock 服务端实际存在 ~5 万次/日软限制 (2026-08-16 实证: day_count 52956
    # 时被服务端拉黑 "黑名单用户"). 设计: 不硬拦截 — 达上限由长任务检查后优雅
    # 停止并提示换热点; IP 变化自动清零计数 + 解除黑名单 (新 IP 不承继旧封禁).

    def _probe_public_ip(self) -> str:
        """探测公网 IP — 失败返回 "" (降级: 不阻断, 视为 IP 未变)."""
        try:
            import urllib.request
            url = _require_cfg("data.rate_limit.baostock_ip_probe_url",
                               "https://myip.ipip.net")
            with urllib.request.urlopen(url, timeout=8) as resp:
                text = resp.read().decode("utf-8", "replace")
            import re
            # IPv4 优先; 双栈网络 (IPv6) 时 ipip 返回 IPv6 — 变化检测只要"变"即可
            m4 = re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
            if m4:
                return m4.group(0)
            m6 = re.search(r"([0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}", text)
            return m6.group(0) if m6 else ""
        except Exception:
            return ""

    def ip_rotated(self) -> tuple:
        """探测公网 IP 并与状态文件 last_ip 比对.

        Returns:
            (rotated: bool, old_ip: str, new_ip: str)
        rotated=True 表示 IP 已变化 — 调用方应执行 reset_day() (清零计数+解除黑名单).
        探测失败 (new_ip="") 时返回 (False, old_ip, "") — 降级按未变化处理.
        """
        st = self._load_state()
        old = st.get("last_ip", "")
        new = self._probe_public_ip()
        if not new:
            return False, old, ""
        return (old != "" and old != new), old, new

    def probe_and_reset_if_rotated(self) -> dict:
        """任务开始前调用: IP 变化则自动清零日计数 + 解除黑名单冷却.

        幂等 (多次调用无副作用); 探测失败不重置 (降级). 首次探测 (无基线) 时
        建立 last_ip 基线, 并解除存量黑名单冷却 — 若服务端仍封, 调用点
        mark_blacklisted() 会再次标记 (冷却重新计时, 无副作用).
        返回 (rotated, 状态快照).
        """
        rotated, old, new = self.ip_rotated()
        if rotated:
            logger.warning(f"baostock 公网 IP 变化 {old} → {new} — 自动清零日计数并解除黑名单")
            st = self.reset_day()
            st["last_ip"] = new
            self._save_state(st)
            try:
                from quant.monitor.alerts import clear_baostock_quota_alert
                clear_baostock_quota_alert()   # v513: 恢复续跑 → 前端横幅消失
            except Exception:
                pass
            return rotated, st
        if new:
            st = self._load_state()
            if st.get("last_ip") != new:
                st["last_ip"] = new
                if st.get("blacklisted_at"):
                    logger.warning(f"baostock 首次建立 IP 基线 {new} — "
                                   "解除存量黑名单冷却 (若服务端仍封将再次标记)")
                    st.pop("blacklisted_at", None)
                    st.pop("blacklist_msg", None)
                    if st.get("day") == time.strftime("%Y-%m-%d"):
                        st["day_count"] = 0
                self._save_state(st)
            return False, st
        return False, self._load_state()

    def day_limit_reached(self) -> tuple:
        """当日请求是否已达上限。

        Returns:
            (reached: bool, count: int, limit: int)
            仅统计报告, 不抛异常 — 由长任务循环检查后自行优雅停止.
        """
        st = self._load_state()
        today = time.strftime("%Y-%m-%d")
        if st.get("day") != today:
            return False, 0, self._per_day
        return st.get("day_count", 0) >= self._per_day, st.get("day_count", 0), self._per_day

    def reset_day(self) -> dict:
        """换热点后重置当日计数与黑名单标记 (新公网 IP 服务端计数从零开始).

        仅清计数, 不动任务互斥锁. 返回重置后的状态快照.
        """
        st = self._load_state()
        st["day"] = time.strftime("%Y-%m-%d")
        st["day_count"] = 0
        st.pop("blacklisted_at", None)
        st.pop("blacklist_msg", None)
        self._save_state(st)
        logger.warning("baostock 日计数已重置 (换热点后) — blacklisted_at 已清除")
        return st

    # ── 任务级互斥 (v511: 防并行叠加 — 封禁根因) ──

    _TASK_FLAG = _STATE_PATH.with_name(".baostock_task.busy")
    _task_depth = [0]   # 模块级重入计数: 同进程嵌套调用不重复抢锁

    def task_busy(self) -> bool:
        """另一拉取任务是否在运行 — 同进程持有不算 busy (嵌套安全)."""
        if self._task_depth[0] > 0:
            return False
        if not self._TASK_FLAG.exists():
            return False
        return self._task_flag_owner_alive()

    def _task_flag_owner_alive(self) -> bool:
        """.busy 文件记录的进程是否存活; 无记录/死亡 → False (残留可接管)."""
        try:
            text = self._TASK_FLAG.read_text()
        except OSError:
            return False
        pid = None
        for part in text.split():
            if part.startswith("pid="):
                try:
                    pid = int(part.split("=")[1])
                except ValueError:
                    pass
        if pid is None or pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _task_try_mark(self, owner: str) -> None:
        """原子抢占任务互斥标记 — 已存在且持有进程存活 → BaostockTaskBusy."""
        self._TASK_FLAG.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._TASK_FLAG, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._task_depth[0] > 0 or not self._task_flag_owner_alive():
                # 同进程重入 或 崩溃残留 (owner 已死) → 接管/重入
                try:
                    self._TASK_FLAG.unlink()
                except FileNotFoundError:
                    pass
                fd = os.open(self._TASK_FLAG, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                prev = self._TASK_FLAG.read_text()[:200] if self._TASK_FLAG.exists() else "?"
                raise BaostockTaskBusy(
                    f"另一 baostock 拉取任务在运行 (owner={prev}), 本任务拒绝并行")
        with os.fdopen(fd, "w") as f:
            f.write(f"{owner} pid={os.getpid()} at={time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"baostock task lock acquired: {owner} pid={os.getpid()}")

    def _task_clear(self, owner: str) -> None:
        """释放任务互斥标记 (仅退出最外层时删除)."""
        if self._task_depth[0] > 0:
            return
        try:
            self._TASK_FLAG.unlink()
        except FileNotFoundError:
            pass
        logger.info(f"baostock task lock released: {owner}")

    @contextlib.contextmanager
    def task_scope(self, owner: str):
        """长任务互斥作用域: 进入抢锁, 退出释放. 并行任务抛 BaostockTaskBusy.

        同进程嵌套调用重入安全 (深度计数); 崩溃残留锁自动接管 (owner pid 探活).
        v513: 仅最外层进入时做换热点检测 (IP 变 → 清零日计数 + 解除黑名单),
        嵌套重入不重复探测 (最后一层探测结果供整个任务使用).
        用法:
            with gate.task_scope("industry_pit"):
                ...  # baostock 请求 (bs_query 内部已做 0.5s 间隔串行)
        """
        self._task_try_mark(owner)
        self._task_depth[0] += 1
        if self._task_depth[0] == 1:
            self.probe_and_reset_if_rotated()
        try:
            yield
        finally:
            self._task_depth[0] -= 1
            self._task_clear(owner)


    @staticmethod
    def _jittered_interval() -> float:
        """±10% 随机抖动 — 避免多个进程同步请求形成规律节奏."""
        base = float(_require_cfg("data.rate_limit.baostock_per_stock_sec", 0.5))
        return base * random.uniform(0.9, 1.1)


def bs_task(owner: str):
    """装饰器: 包裹 baostock 长任务函数 — 并行任务抛 BaostockTaskBusy (fail-fast).

    用法:
        @bs_task("update_daily_baostock")
        def _fetch_baostock_daily(self, symbols, start_date):
            ...
    """
    def _deco(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            with gate.task_scope(owner):
                return fn(*args, **kwargs)
        return _wrapper
    return _deco


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