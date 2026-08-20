"""腾讯/东方财富 数据源实现 — TLS 指纹伪装."""

from __future__ import annotations
import curl_cffi.requests as _req
import json
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("data.sources.tencent")


class TencentSource(BaseDataSource):
    """腾讯/东方财富 实时行情 + 历史 K 线.

    使用 curl_cffi 模拟 Chrome 131 TLS 指纹, 绕过 eastmoney CDN JA3 检测.
    域名: push2.eastmoney.com (82.push2his 已被定向 DNS 封禁).
    """

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._session = _req.Session()

    def get_source_type(self) -> str:
        return "tencent"

    def _tc_code(self, sym: str) -> str:
        if sym.startswith("6"):
            return f"1.{sym}"
        return f"0.{sym}"

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        operation = kwargs.pop("operation", "daily")

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            elif operation == "quotes":
                return self._fetch_quotes(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except Exception as e:
            logger.exception(f"[tencent] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )

    def _fetch_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取历史 K 线(前复权)."""
        from datetime import datetime
        from quant.utils.date import to_compact, to_str

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        all_rows = []

        for sym in symbols:
            code = self._tc_code(sym)
            try:
                r = self._session.get(
                    "https://push2.eastmoney.com/api/qt/stock/kline/get",
                    params={
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                        "ut": "7eea3edcaed734bea9cbfc24409ed989",
                        "klt": "101", "fqt": "1", "secid": code,
                        "beg": to_compact(start_date), "end": to_compact(end),
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=_require_cfg("data.http_timeout.tencent"),
                    impersonate="chrome131",
                )
                if r.status_code != 200:
                    continue

                data = r.json().get("data", {})
                klines = data.get("klines")
                if not klines:
                    continue

                for k_str in klines:
                    p = k_str.split(",")
                    d = p[0]
                    if d < start_date:
                        continue
                    all_rows.append({
                        "symbol": sym,
                        "date": to_str(d),
                        "open": float(p[1]),
                        "high": float(p[3]),
                        "low": float(p[4]),
                        "close": float(p[2]),
                        "volume": float(p[5]),              # 手
                        "amount": float(p[6]) / 1000 if len(p) > 6 and p[6] else 0.0,  # 元→千元
                        "turnover": 0.0,
                    })
            except Exception:
                logger.debug(f"[tencent] {sym} request failed")
                continue

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_quotes(self, symbols: list[str]) -> DataSourceResult:
        """获取实时行情(批量)."""
        import urllib.parse

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        codes = ",".join(self._tc_code(s) for s in symbols)
        try:
            r = self._session.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={
                    "fltt": "2",
                    "invt": "2",
                    "fields": "f43,f57,f58,f60,f162,f167,f168,f170,f171",
                    "secids": codes,
                    "ut": "7eea3edcaed734bea9cbfc24409ed989",
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=_require_cfg("data.http_timeout.tencent"),
                impersonate="chrome131",
            )
            if r.status_code != 200:
                return DataSourceResult(success=False, error=f"http {r.status_code}")

            data = r.json().get("data", {})
            diff = data.get("diff", [])
            rows = []
            for item in diff:
                sym = item.get("f12", "")
                rows.append({
                    "symbol": sym,
                    "price": float(item.get("f43", 0)) / 100,  # 当前价(分→元)
                    "change_pct": float(item.get("f170", 0)) / 100,  # 涨跌幅(%)
                    "volume": float(item.get("f57", 0)),  # 成交量(手)
                    "amount": float(item.get("f58", 0)) / 10000,  # 成交额(万元)
                    "turnover": float(item.get("f168", 0)) / 100,  # 换手率(%)
                    "high": float(item.get("f44", 0)) / 100,
                    "low": float(item.get("f45", 0)) / 100,
                    "open": float(item.get("f46", 0)) / 100,
                    "prev_close": float(item.get("f60", 0)) / 100,
                })
            return DataSourceResult(success=True, data=rows, rows_affected=len(rows))
        except Exception as e:
            return DataSourceResult(success=False, error=str(e), error_code=type(e).__name__)

    def _health_check_impl(self) -> bool:
        try:
            r = self._session.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": "1.000001", "ut": "7eea3edcaed734bea9cbfc24409ed989"},
                timeout=5,
                impersonate="chrome131",
            )
            return r.status_code == 200
        except Exception:
            return False