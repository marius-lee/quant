"""Baostock 数据源实现 — 集成现有防封禁机制 + Akshare 回退.

提供: 日线行情(qfq)、行业分类、复权因子、退市股、指数行情、
       资金流、融资融券、龙虎榜、分红、股东增减持、质押、财务报表.
内置跨进程限流 + 黑名单熔断 + IP 变化自动检测.
Akshare 作为回退源补充 Baostock 缺失接口.
"""

from __future__ import annotations
import time
from quant.data.sources.base import BaseDataSource, DataSourceConfig, DataSourceResult
from quant.utils.baostock_gate import gate as bs_gate, bs_query, BaostockBlacklisted, BaostockQuotaExceeded
from quant.utils.logger import get_logger

logger = get_logger("data.sources.baostock")


class BaostockSource(BaseDataSource):
    """Baostock (证券宝) 数据源 — 主源 + Akshare 回退."""

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._akshare_fallback = True

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
            elif operation == "fund_flow":
                return self._fetch_fund_flow(**kwargs)
            elif operation == "margin":
                return self._fetch_margin(**kwargs)
            elif operation == "lhb":
                return self._fetch_lhb(**kwargs)
            elif operation == "dividend":
                return self._fetch_dividend(**kwargs)
            elif operation == "holder_trade":
                return self._fetch_holder_trade(**kwargs)
            elif operation == "pledge":
                return self._fetch_pledge(**kwargs)
            elif operation == "income":
                return self._fetch_income(**kwargs)
            elif operation == "balance":
                return self._fetch_balance(**kwargs)
            elif operation == "cashflow":
                return self._fetch_cashflow(**kwargs)
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

    def _akshare_fallback(self, operation: str, func, *args, **kwargs) -> DataSourceResult:
        """Akshare 回退包装器."""
        if not self._akshare_fallback:
            return DataSourceResult(success=False, error="akshare fallback disabled", error_code="FALLBACK_DISABLED")
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"[baostock] akshare fallback {operation} failed: {e}")
            return DataSourceResult(success=False, error=str(e), error_code=type(e).__name__)

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
                # 回退到 akshare
                return self._akshare_fallback("daily", self._akshare_daily, symbols, start_date, end_date)

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

    def _akshare_daily(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """Akshare 回退: 获取前复权日线."""
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
                logger.warning(f"[akshare fallback] daily {sym} failed: {e}")
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

            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_industry(self) -> DataSourceResult:
        """获取行业分类."""
        rs = bs_query("query_stock_industry")
        if rs.error_code != "0":
            return self._akshare_fallback("industry", self._akshare_industry)

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

    def _akshare_industry(self) -> DataSourceResult:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)
        rows = df.to_dict(orient="records")
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
            return self._akshare_fallback("index_daily", self._akshare_index_daily, index_code, start_date, end_date)

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

    def _akshare_index_daily(self, index_code: str, start_date: str, end_date: str | None = None) -> DataSourceResult:
        import akshare as ak
        from datetime import datetime
        from quant.utils.date import to_compact

        end = end_date or datetime.today().strftime("%Y%m%d")
        start = to_compact(start_date)

        try:
            df = ak.index_zh_a_hist(symbol=index_code, period="daily", start_date=start, end_date=end)
        except Exception as e:
            logger.warning(f"[akshare fallback] index_daily {index_code} failed: {e}")
            return DataSourceResult(success=True, data=[], rows_affected=0)

        if df is None or df.empty:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        rows = df.to_dict(orient="records")
        return DataSourceResult(success=True, data=rows, rows_affected=len(rows))

    def _fetch_fund_flow(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取资金流 — 仅 Akshare."""
        return self._akshare_fallback("fund_flow", self._akshare_fund_flow, symbols, start_date, end_date)

    def _akshare_fund_flow(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_individual_fund_flow(symbol=sym, market="a")
            except Exception as e:
                logger.warning(f"[akshare fallback] fund_flow {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(2.0)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_margin(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取融资融券 — 仅 Akshare."""
        return self._akshare_fallback("margin", self._akshare_margin, symbols, start_date, end_date)

    def _akshare_margin(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_margin_detail_em(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] margin {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(2.0)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_lhb(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取龙虎榜 — 仅 Akshare."""
        return self._akshare_fallback("lhb", self._akshare_lhb, symbols, start_date, end_date)

    def _akshare_lhb(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_lhb_detail_em(symbol=sym, start_date=start, end_date=end)
            except Exception as e:
                logger.warning(f"[akshare fallback] lhb {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_dividend(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取分红 — 仅 Akshare."""
        return self._akshare_fallback("dividend", self._akshare_dividend, symbols, start_date, end_date)

    def _akshare_dividend(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_dividend_cninfo(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] dividend {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_holder_trade(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取股东增减持 — 仅 Akshare."""
        return self._akshare_fallback("holder_trade", self._akshare_holder_trade, symbols, start_date, end_date)

    def _akshare_holder_trade(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_holder_trade_em(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] holder_trade {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_pledge(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
        """获取质押 — 仅 Akshare."""
        return self._akshare_fallback("pledge", self._akshare_pledge, symbols, start_date, end_date)

    def _akshare_pledge(self, symbols: list[str], start_date: str, end_date: str | None = None) -> DataSourceResult:
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
                df = ak.stock_pledge_ratio_em(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] pledge {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_income(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        """获取利润表 — 仅 Akshare."""
        return self._akshare_fallback("income", self._akshare_income, symbols, period)

    def _akshare_income(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        import akshare as ak

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        all_rows = []

        for sym in symbols:
            try:
                df = ak.stock_profit_forecast(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] income {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_balance(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        """获取资产负债表 — 仅 Akshare."""
        return self._akshare_fallback("balance", self._akshare_balance, symbols, period)

    def _akshare_balance(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        import akshare as ak

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        all_rows = []

        for sym in symbols:
            try:
                df = ak.stock_balance_sheet_by_report_em(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] balance {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _fetch_cashflow(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        """获取现金流量表 — 仅 Akshare."""
        return self._akshare_fallback("cashflow", self._akshare_cashflow, symbols, period)

    def _akshare_cashflow(self, symbols: list[str], period: str | None = None) -> DataSourceResult:
        import akshare as ak

        if not symbols:
            return DataSourceResult(success=True, data=[], rows_affected=0)

        all_rows = []

        for sym in symbols:
            try:
                df = ak.stock_cash_flow_by_report_em(symbol=sym)
            except Exception as e:
                logger.warning(f"[akshare fallback] cashflow {sym} failed: {e}")
                continue

            if df is None or df.empty:
                continue

            df["symbol"] = sym
            all_rows.extend(df.to_dict(orient="records"))
            time.sleep(1.5)

        return DataSourceResult(success=True, data=all_rows, rows_affected=len(all_rows))

    def _bs_code(self, sym: str) -> str:
        """6位代码 → baostock 格式."""
        if sym.startswith(("6", "9")):
            return f"sh.{sym}"
        return f"sz.{sym}"

    def _health_check_impl(self) -> bool:
        """健康检查: 尝试登录并查询单只股票."""
        try:
            lg = bs_query("login")
            if lg.error_code != "0":
                return False
            from datetime import datetime
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