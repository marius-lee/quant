"""Akshare 数据源实现 — TLS 指纹伪装 + 限流."""

from __future__ import annotations
import sys
import time
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.logger import get_logger

logger = get_logger("data.sources.akshare")


class AkshareSource(BaseDataSource):
    """Akshare 数据源 — 免费全品类.

    提供: 日线行情(qfq)、基本面、资金流、龙虎榜、两融、指数、宏观等.
    内置 TLS 指纹伪装 (curl_cffi 替换 requests) 绕过东财 CDN 检测.
    """

    def get_source_type(self) -> str:
        return "akshare"

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        operation = kwargs.pop("operation", "daily")

        # TLS 指纹伪装: 替换 requests 为 curl_cffi
        import curl_cffi.requests as _curl_requests
        import requests as _orig_requests
        sys.modules['requests'] = _curl_requests

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            elif operation == "stock_list":
                return self._fetch_stock_list(**kwargs)
            elif operation == "industry":
                return self._fetch_industry(**kwargs)
            elif operation == "fund_flow":
                return self._fetch_fund_flow(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except Exception as e:
            logger.exception(f"[akshare] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )
        finally:
            # 恢复原始 requests
            sys.modules['requests'] = _orig_requests

    def _fetch_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取前复权日线 — 逐只拉取."""
        import akshare as ak
        from datetime import datetime
        from quant.utils.date import to_compact

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y%m%d")
        start = to_compact(start_date)
        all_rows = []

        for sym in symbols:
            try:
                df = ak.stock_zh_a_hist(
                    symbol=sym,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="qfq",
                )
            except Exception as e:
                logger.warning(f"[akshare] {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                all_rows.append({
                    "symbol": str(row["股票代码"]),
                    "date": str(row["日期"]),
                    "open": float(row.get("开盘", 0) or 0),
                    "high": float(row.get("最高", 0) or 0),
                    "low": float(row.get("最低", 0) or 0),
                    "close": float(row.get("收盘", 0) or 0),
                    "volume": float(row.get("成交量", 0) or 0),      # 手
                    "amount": float(row.get("成交额", 0) or 0) / 1000,  # 元→千元
                    "turnover": float(row.get("换手率", 0) or 0),
                })

            time.sleep(1.5)  # 单只间隔

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_stock_list(self) -> DataSourceResult:
        """获取全 A 股列表."""
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)
        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_industry(self) -> DataSourceResult:
        """获取行业分类."""
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)
        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_fund_flow(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取个股资金流."""
        import akshare as ak
        from datetime import datetime
        from quant.utils.date import to_compact

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y%m%d")
        start = to_compact(start_date)
        all_rows = []

        for sym in symbols:
            try:
                df = ak.stock_individual_fund_flow(
                    symbol=sym,
                    market="a",
                )
            except Exception as e:
                logger.warning(f"[akshare] fund_flow {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(2.0)  # 资金流接口更严格

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _health_check_impl(self) -> bool:
        """健康检查."""
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(
                symbol="000001",
                period="daily",
                start_date=datetime.today().strftime("%Y%m%d"),
                end_date=datetime.today().strftime("%Y%m%d"),
                adjust="qfq",
            )
            return df is not None and not df.empty
        except Exception:
            return False