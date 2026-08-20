"""Baostock 数据源实现 — 集成现有防封禁机制."""

from __future__ import annotations
import time
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.baostock_gate import gate as bs_gate, bs_query, BaostockBlacklisted, BaostockQuotaExceeded
from quant.utils.logger import get_logger

logger = get_logger("data.sources.baostock")


class BaostockSource(BaseDataSource):
    """Baostock (证券宝) 数据源.

    提供: 日线行情(qfq)、行业分类、复权因子、退市股、指数行情.
    内置跨进程限流 + 黑名单熔断 + IP 变化自动检测.
    """

    def get_source_type(self) -> str:
        return "baostock"

    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        operation = kwargs.pop("operation", "daily")

        try:
            if operation == "daily":
                return self._fetch_daily(**kwargs)
            elif operation == "industry":
                return self._fetch_industry(**kwargs)
            elif operation == "adj_factor":
                return self._fetch_adj_factor(**kwargs)
            elif operation == "delisted":
                return self._fetch_delisted(**kwargs)
            elif operation == "index_daily":
                return self._fetch_index_daily(**kwargs)
            else:
                return DataSourceResult(
                    success=False,
                    error=f"unsupported operation: {operation}",
                    error_code="UNSUPPORTED_OPERATION",
                )
        except BaostockBlacklisted as e:
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code="BLACKLISTED",
            )
        except BaostockQuotaExceeded as e:
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code="QUOTA_EXCEEDED",
            )
        except Exception as e:
            logger.exception(f"[baostock] {operation} failed")
            return DataSourceResult(
                success=False,
                error=str(e),
                error_code=type(e).__name__,
            )

    def _bs_code(self, sym: str) -> str:
        """6位代码 → baostock 格式."""
        if sym.startswith(("6", "9")):
            return f"sh.{sym}"
        return f"sz.{sym}"

    def _fetch_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取前复权日线行情."""
        from datetime import datetime

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        all_rows = []

        for sym in symbols:
            bs_code = self._bs_code(sym)
            rs = bs_query(
                "query_history_k_data_plus",
                code=bs_code,
                fields="date,open,high,low,close,volume,amount,turn",
                start_date=start_date,
                end_date=end,
                frequency="d",
                adjustflag="2",  # 前复权
            )

            if rs.error_code != "0":
                logger.warning(f"[baostock] daily {bs_code}: {rs.error_msg}")
                continue

            while rs.next():
                row_data = rs.get_row_data()
                try:
                    all_rows.append({
                        "symbol": sym,
                        "date": row_data[0],
                        "open": float(row_data[1]),
                        "high": float(row_data[2]),
                        "low": float(row_data[3]),
                        "close": float(row_data[4]),
                        "volume": float(row_data[5]) / 100.0,    # 股→手
                        "amount": float(row_data[6]) / 1000.0,    # 元→千元
                        "turnover": float(row_data[7]) if row_data[7] else 0.0,
                    })
                except (ValueError, IndexError):
                    continue

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_industry(self) -> DataSourceResult:
        """获取行业分类."""
        rs = bs_query("query_stock_industry")
        if rs.error_code != "0":
            return DataSourceResult(success=False, error=rs.error_msg, error_code=rs.error_code)

        rows = []
        while rs.next():
            row_data = rs.get_row_data()
            code = row_data[1]  # code 列
            industry = row_data[3]  # industry 列
            if "." in code:
                sym = code.split(".")[-1]
            else:
                sym = code
            if len(sym) == 6 and industry:
                rows.append({"symbol": sym, "industry": industry})

        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_adj_factor(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取复权因子."""
        from datetime import datetime

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        all_rows = []

        for sym in symbols:
            bs_code = self._bs_code(sym)
            rs = bs_query(
                "query_adjust_factor",
                code=bs_code,
                start_date=start_date,
                end_date=end,
            )

            if rs.error_code != "0":
                continue

            while rs.next():
                row_data = rs.get_row_data()
                try:
                    factor_val = float(row_data[4]) if row_data[4] else None
                    if factor_val is not None:
                        all_rows.append({
                            "symbol": sym,
                            "date": row_data[1],
                            "factor": factor_val,
                        })
                except (ValueError, IndexError):
                    continue

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_delisted(self) -> DataSourceResult:
        """获取退市股列表."""
        import akshare as ak
        import pandas as pd

        try:
            df = ak.stock_info_a_delist()
            if df is None or df.empty:
                df_sh = ak.stock_info_sh_delist()
                df_sz = ak.stock_info_sz_delist()

                def _norm(d, sym_col, name_col, date_col):
                    out = pd.DataFrame()
                    out["symbol"] = d[sym_col].astype(str).str.zfill(6)
                    out["name"] = d[name_col]
                    out["delist_date"] = d[date_col]
                    return out

                df = pd.concat([
                    _norm(df_sh, "公司代码", "公司简称", "暂停上市日期"),
                    _norm(df_sz, "证券代码", "证券简称", "终止上市日期"),
                ], ignore_index=True)

            rows = df.to_dict(orient="records")
            return DataSourceResult(success=True, data=rows, rows_affected=len(rows))
        except Exception as e:
            return DataSourceResult(success=False, error=str(e), error_code=type(e).__name__)

    def _fetch_index_daily(self, index_code: str, start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取指数日线."""
        from datetime import datetime

        end = end_date or datetime.today().strftime("%Y-%m-%d")
        rs = bs_query(
            "query_history_k_data_plus",
            code=index_code,
            fields="date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end,
            frequency="d",
            adjustflag="3",
        )

        if rs.error_code != "0":
            return DataSourceResult(success=False, error=rs.error_msg, error_code=rs.error_code)

        rows = []
        while rs.next():
            row_data = rs.get_row_data()
            try:
                rows.append({
                    "date": row_data[0],
                    "open": float(row_data[1]),
                    "high": float(row_data[2]),
                    "low": float(row_data[3]),
                    "close": float(row_data[4]),
                    "volume": float(row_data[5]),
                    "amount": float(row_data[6]),
                })
            except (ValueError, IndexError):
                continue

        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _health_check_impl(self) -> bool:
        """健康检查: 尝试登录并查询单只股票."""
        try:
            lg = bs_query("login")
            if lg.error_code != "0":
                return False
            rs = bs_query(
                "query_history_k_data_plus",
                code="sh.000001",
                fields="date,close",
                start_date=datetime.today().strftime("%Y-%m-%d"),
                end_date=datetime.today().strftime("%Y-%m-%d"),
            )
            return rs.error_code == "0"
        except Exception:
            return False
        finally:
            try:
                import baostock as bs
                bs.logout()
            except Exception:
                pass