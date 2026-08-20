"""Tushare 数据源实现."""

from __future__ import annotations
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.logger import get_logger

logger = get_logger("data.sources.tushare")


class TushareSource(BaseDataSource):
    """Tushare Pro 数据源.

    提供: 日线行情、基本面、复权因子、龙虎榜、融资融券、北向资金、分红、财报等.
    """

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._pro = None
        self._token = None

    def get_source_type(self) -> str:
        return "tushare"

    def _init_client(self):
        """延迟初始化 Tushare 客户端."""
        if self._pro is not None:
            return

        import tushare as ts
        from quant.config.constants import _require_cfg

        self._token = _require_cfg("data.tushare_token")
        ts.set_token(self._token)
        self._pro = ts.pro_api(timeout=_require_cfg("data.http_timeout.tushare"))

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        """Tushare 统一获取入口.

        支持的操作类型:
          - daily: 日线行情
          - adj_factor: 复权因子
          - stock_basic: 股票列表
          - fund_flow: 资金流
          - margin: 融资融券
          - ... 更多见 Tushare 文档
        """
        self._init_client()
        operation = kwargs.pop("operation", "daily")

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            elif operation == "adj_factor":
                return self._fetch_adj_factor(**kwargs)
            elif operation == "stock_basic":
                return self._fetch_stock_basic(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except Exception as e:
            logger.exception(f"[tushare] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )

    def _fetch_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取日线行情."""
        import pandas as pd
        from quant.utils.date import to_compact
        from datetime import datetime

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        ts_codes = []
        for s in symbols:
            if s.startswith("92"):
                ts_codes.append(f"{s}.BJ")
            elif s.startswith(("6", "9", "68")):
                ts_codes.append(f"{s}.SH")
            else:
                ts_codes.append(f"{s}.SZ")

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        df = self._pro.daily(
            ts_code=",".join(ts_codes),
            start_date=to_compact(start_date),
            end_date=to_compact(end),
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )

        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        df["symbol"] = df["ts_code"].str.split(".").str[0]
        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_adj_factor(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取复权因子."""
        from datetime import datetime
        from quant.utils.date import to_compact

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        ts_codes = []
        for s in symbols:
            if s.startswith("92"):
                ts_codes.append(f"{s}.BJ")
            elif s.startswith(("6", "9", "68")):
                ts_codes.append(f"{s}.SH")
            else:
                ts_codes.append(f"{s}.SZ")

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        df = self._pro.adj_factor(
            ts_code=",".join(ts_codes),
            start_date=to_compact(start_date),
            end_date=to_compact(end),
            fields="ts_code,trade_date,adj_factor",
        )

        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        df["symbol"] = df["ts_code"].str.split(".").str[0]
        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_stock_basic(self) -> DataSourceResult:
        """获取股票基础信息."""
        df = self._pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,list_date,market",
        )
        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)
        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _health_check_impl(self) -> bool:
        """健康检查: 尝试获取单只股票最新行情."""
        try:
            self._init_client()
            df = self._pro.daily(
                ts_code="000001.SZ",
                start_date=datetime.today().strftime("%Y%m%d"),
                end_date=datetime.today().strftime("%Y%m%d"),
            )
            return df is not None
        except Exception:
            return False