"""TickFlow 数据源实现 — 实时行情 + 历史 K 线."""

from __future__ import annotations
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.logger import get_logger

logger = get_logger("data.sources.tickflow")


class TickFlowSource(BaseDataSource):
    """TickFlow 数据源.

    提供: 实时行情、历史 K 线、Level-2 数据、期权行情.
    支持免费版(仅历史日K)和注册版(批量 K 线 + 实时行情).
    """

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._free_client = None
        self._pro_client = None
        self._batch_no_perm = False

    def get_source_type(self) -> str:
        return "tickflow"

    def _get_free_client(self):
        if self._free_client is None:
            from tickflow import TickFlow
            self._free_client = TickFlow.free()
        return self._free_client

    def _get_pro_client(self):
        if self._pro_client is None:
            from tickflow import TickFlow
            from quant.config.constants import _require_cfg
            api_key = _require_cfg("data.tickflow_api_key")
            self._pro_client = TickFlow(api_key=api_key)
        return self._pro_client

    def _tf_code(self, sym: str) -> str:
        if sym.startswith("920"):
            return f"{sym}.BJ"
        if sym.startswith(("6", "9", "68")):
            return f"{sym}.SH"
        return f"{sym}.SZ"

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        operation = kwargs.pop("operation", "daily")

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            elif operation == "quotes":
                return self._fetch_quotes(**kwargs)
            elif operation == "klines_batch":
                return self._fetch_klines_batch(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except Exception as e:
            logger.exception(f"[tickflow] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )

    def _fetch_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取日线 — 优先注册版批量, 失败回退免费版."""
        from datetime import datetime
        from quant.utils.date import to_compact
        import pandas as pd

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        codes = [self._tf_code(s) for s in symbols]

        # 当天数据直接用 quotes
        if to_compact(start_date) >= to_compact(datetime.today().strftime("%Y-%m-%d")):
            return self._fetch_quotes(symbols, datetime.today().strftime("%Y-%m-%d"))

        # 尝试注册版批量 K 线
        dfs = None
        if not self._batch_no_perm:
            try:
                client = self._get_pro_client()
                dfs = client.klines.batch(
                    codes, period="1d", count=10000,
                    as_dataframe=True, show_progress=False,
                )
                logger.info(f"[tickflow] pro batch klines OK ({len(codes)} codes)")
            except Exception as e:
                from tickflow import PermissionError as TFPermError
                if isinstance(e, TFPermError):
                    self._batch_no_perm = True
                logger.warning(f"[tickflow] pro batch failed: {e} → free tier")

        # 免费版回退
        if dfs is None:
            client = self._get_free_client()
            dfs = client.klines.batch(
                codes, period="1d", count=10000,
                as_dataframe=True, show_progress=False,
            )

        # 注意: tickflow 返回未复权数据, 需配合本地 adj_factor 转 qfq
        # 此处返回原始数据, 上层负责复权转换
        all_rows = []
        for code, df in dfs.items():
            if df.empty:
                continue
            sym = code.split(".")[0]
            for _, row in df.iterrows():
                all_rows.append({
                    "symbol": sym,
                    "date": str(row.get("trade_date", ""))[:10],
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),      # 手
                    "amount": float(row.get("amount", 0)) / 1000,  # 元→千元
                })

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_quotes(self, symbols: list[str], date: str) -> DataSourceResult:
        """获取实时行情(含 turnover_rate) — 仅注册版."""
        from tickflow import TickFlow
        from quant.config.constants import _require_cfg

        api_key = _require_cfg("data.tickflow_api_key")
        tf = TickFlow(api_key=api_key)

        codes = [self._tf_code(s) for s in symbols]
        all_rows = []

        # quotes API 单次最多 5 只
        for i in range(0, len(codes), 5):
            chunk = codes[i:i+5]
            try:
                df = tf.quotes.get(symbols=chunk, as_dataframe=True)
            except Exception as e:
                logger.warning(f"[tickflow] quotes chunk {i} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            for _, q in df.iterrows():
                sym = str(q.get("symbol", "")).split(".")[0]
                all_rows.append({
                    "symbol": sym,
                    "date": date,
                    "open": float(q.get("open", 0)),
                    "high": float(q.get("high", 0)),
                    "low": float(q.get("low", 0)),
                    "close": float(q.get("last_price", 0)),
                    "volume": float(q.get("volume", 0)),
                    "amount": float(q.get("amount", 0)) / 1000,
                    "turnover": float(q.get("ext.turnover_rate", 0) or 0),
                })

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_klines_batch(self, symbols: list[str], period: str = "1d", count: int = 10000) -> DataSourceResult:
        """批量获取 K 线(内部用)."""
        return self._fetch_daily(symbols, "2020-01-01")  # 简化实现

    def _health_check_impl(self) -> bool:
        try:
            client = self._get_free_client()
            df = client.klines.batch(
                ["000001.SZ"], period="1d", count=1,
                as_dataframe=True, show_progress=False,
            )
            return df is not None and "000001.SZ" in df and not df["000001.SZ"].empty
        except Exception:
            return False