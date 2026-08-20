"""Pytdx (通达信) 数据源实现 — TCP 直连, 无需 API Key."""

from __future__ import annotations
import socket
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("data.sources.pytdx")


class PytdxSource(BaseDataSource):
    """Pytdx 数据源 — 通达信标准行情协议.

    特点:
      - TCP 直连 180.153.18.170:7709, 无需 API Key
      - 数据质量可靠, 稳定运行 30 年+, 覆盖绝大多数券商
      - 无频率限制, 但逐只拉取较慢
      - 不提供换手率, 需手算前复权因子
    """

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._api = None

    def get_source_type(self) -> str:
        return "pytdx"

    def _get_api(self):
        if self._api is not None:
            return self._api

        from pytdx.hq import TdxHq_API
        self._api = TdxHq_API()

        # Socket 预探测
        connect_timeout = _require_cfg("data.pytdx.connect_timeout")
        sock = socket.create_connection(("180.153.18.170", 7709), timeout=connect_timeout)
        sock.close()

        if not self._api.connect("180.153.18.170", 7709):
            raise RuntimeError("pytdx: server unreachable")

        return self._api

    def _market_code(self, sym: str) -> int:
        if sym.startswith(("0", "2", "3")):
            return 0  # 深圳
        return 1  # 上海

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        operation = kwargs.pop("operation", "daily")

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except Exception as e:
            logger.exception(f"[pytdx] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )
        finally:
            if self._api:
                try:
                    self._api.disconnect()
                except Exception:
                    pass
                self._api = None

    def _fetch_daily(self, symbols: list[str], start_date: str) -> DataSourceResult:
        """获取前复权日线 — 逐只拉取 + 除权除息手算复权."""
        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        api = self._get_api()
        all_rows = []

        for sym in symbols:
            market = self._market_code(sym)

            # 1. 获取除权除息记录
            xdxr = api.get_xdxr_info(market, sym)
            adj_map = {}
            if xdxr:
                events = []
                for r in xdxr:
                    songzhuan = float(r.get('songzhuangu', 0) or 0)
                    if songzhuan > 0:
                        d = '%d-%02d-%02d' % (r['year'], r['month'], r['day'])
                        events.append((d, 1 + songzhuan / 10))
                if events:
                    events.sort(key=lambda x: x[0])
                    cum = 1.0
                    for d, ratio in events:
                        cum *= ratio
                        adj_map[d] = cum

            # 2. 获取日线并应用前复权
            bars = api.get_security_bars(9, market, sym, 0, 2000)
            if not bars:
                continue

            for b in bars:
                d = '%d-%02d-%02d' % (b['year'], b['month'], b['day'])
                if d < start_date:
                    continue

                o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
                vol = float(b['vol'])
                amt = float(b['amount'])

                # 前复权因子计算
                factor = 1.0
                if adj_map:
                    cum_before = 1.0
                    cum_all = 1.0
                    found = False
                    for ed, ratio in sorted(adj_map.items()):
                        cum_all = ratio
                        if ed <= d:
                            cum_before = ratio
                            found = True
                    if found and cum_before > 0:
                        factor = cum_before / cum_all
                    else:
                        factor = 1.0 / cum_all

                all_rows.append({
                    "symbol": sym,
                    "date": d,
                    "open": round(o * factor, 4),
                    "high": round(h * factor, 4),
                    "low": round(l * factor, 4),
                    "close": round(c * factor, 4),
                    "volume": vol,              # 手
                    "amount": amt / 1000,       # 元→千元
                    "turnover": 0.0,
                })

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _health_check_impl(self) -> bool:
        try:
            api = self._get_api()
            bars = api.get_security_bars(9, 1, "000001", 0, 1)
            return bars is not None and len(bars) > 0
        except Exception:
            return False