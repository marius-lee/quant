"""CDC 性能优化 — 批量 UPSERT + 列裁剪 + 向量化 + 内存保护."""

from __future__ import annotations
import sqlite3
import duckdb
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Iterator, Tuple
from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.performance")


@dataclass
class BatchUpsertConfig:
    """批量 UPSERT 配置."""
    batch_size: int = 10000           # 单批次行数
    use_columnar: bool = True         # 使用列式批量插入
    enable_vectorized: bool = True    # 启用 DuckDB 向量化
    prune_columns: bool = True        # 裁剪非必要列
    use_prepared: bool = True         # 使用预编译语句


class VectorizedUpserter:
    """向量化批量 UPSERT 引擎.

    核心优化:
    1. 列式批量构建 - 避免逐行 Python 循环
    2. DuckDB 向量化执行 - 利用 SIMD 指令集
    3. 预编译语句复用 - 减少解析开销
    4. 列裁剪 - 仅传输必要列
    """

    def __init__(
        self,
        duckdb_conn: duckdb.DuckDBPyConnection,
        config: Optional[BatchUpsertConfig] = None,
    ):
        self.conn = conn
        self.config = config or BatchUpsertConfig()
        self._prepared_statements: Dict[str, Any] = {}

    def upsert_batch(
        self,
        table: str,
        pk_cols: List[str],
        rows: List[Dict[str, Any]],
        columns: Optional[List[str]] = None,
    ) -> int:
        """向量化批量 UPSERT.

        Args:
            table: 目标表名
            pk_cols: 主键列列表
            rows: 行数据列表 (字典列表)
            columns: 可选，指定参与列 (裁剪)

        Returns:
            成功写入行数
        """
        if not rows:
            return 0

        # 列裁剪
        if self.config.prune_columns and columns:
            pk_set = set(pk_cols) if isinstance(pk_cols, list) else set()
            relevant_cols = [c for c in columns if c in pk_cols or c in rows[0]]
            rows = [{k: r[k] for k in relevant_cols if k in r} for r in rows]
        else:
            relevant_cols = list(rows[0].keys())

        # 列式转置: {col: [val1, val2, ...]} - 适合 DuckDB 向量化
        columnar = {col: [row.get(col) for row in rows] for col in relevant_cols}
        num_rows = len(rows)

        # 构建 UPSERT SQL
        pk_cols = pk_cols if isinstance(pk_cols, list) else [pk_cols]
        col_list = ", ".join(relevant_cols)
        pk_list = ", ".join(pk_cols) if isinstance(pk_cols, list) else pk_cols
        update_cols = [c for c in relevant_cols if c not in pk_cols]
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols) if update_cols else ""

        # 使用 DuckDB 的批量插入 API (向量化)
        # DuckDB 支持 FROM 子查询批量插入
        columns_str = ", ".join(relevant_cols)
        placeholders = ", ".join(["?" for _ in relevant_cols])
        pk_list = ", ".join(pk_cols) if isinstance(pk_cols, list) else pk_cols

        if self.config.enable_vectorized:
            # 向量化路径: 使用 DuckDB 的 FROM 批量插入
            # 构建 VALUES 子句的列式数据
            columns_str = ", ".join(relevant_cols)
            placeholders = ", ".join(["?" for _ in relevant_cols])

            # 使用 DuckDB 的 executemany 或批量插入
            # 这里使用更高效的列式批量插入
            self._execute_batch_upsert(table, pk_cols, relevant_cols, columnar_data)
        else:
            # 回退到逐行执行
            for row_data in zip(*[columnar[col] for col in relevant_cols]):
                row_dict = dict(zip(relevant_cols, row_data))
                self._upsert_single(table, pk_cols, row_data)

        return len(rows)

    def _execute_batch_upsert(self, table: str, pk_cols: List[str], columns: List[str], columnar: Dict[str, List]):
        """执行列式批量 UPSERT."""
        # 使用 DuckDB 的 unnest + lateral join 进行向量化 UPSERT
        # 这是 DuckDB 最高效的批量写入方式
        import duckdb

        cols = list(columnar.keys())
        num_rows = len(next(iter(columnar.values())))

        # 构建列式数据表达式
        # DuckDB 支持: INSERT INTO table SELECT * FROM (SELECT unnest(arr1) AS col1, unnest(arr2) AS col2, ...)
        arrays = {col: self.duckdb_conn.execute(f"SELECT {col} FROM (SELECT unnest(?) AS {col})", [self.conn.execute(f"SELECT ?").fetchall()]) for col in relevant_cols}
        
        # 简化版: 使用 executemany (DuckDB 1.0+ 支持)
        placeholders = ", ".join(["?" for _ in range(len(pk_cols) + len([c for c in relevant_cols if c not in pk_cols]))])
        pk_cols_str = ", ".join(pk_cols)
        update_cols = [c for c in relevant_cols if c not in pk_cols]
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols) if [c for c in relevant_cols if c not in pk_cols] else ""

        # 使用 DuckDB 的高效批量插入
        self.conn.executemany(
            f"""
            INSERT INTO table_name ({', '.join(relevant_cols)})
            VALUES ({', '.join(['?' for _ in relevant_cols])})
            ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET
            {', '.join(f'{c} = EXCLUDED.{c}' for c in [c for c in relevant_cols if c not in pk_cols])}
            """,
            [tuple(row[col] for col in relevant_cols) for row in zip(*[columnar[c] for c in relevant_cols])]
        )

    def upsert_single(self, table: str, pk_cols: List[str], row: Dict[str, Any]) -> bool:
        """单行 UPSERT (回退模式)."""
        pk_cols = pk_cols if isinstance(pk_cols, list) else [pk_cols]
        cols = list(row.keys())
        placeholders = ", ".join(["?" for _ in row])
        cols_str = ", ".join(row.keys())
        pk_list = ", ".join(pk_cols) if isinstance(pk_cols, list) else pk_cols
        update_cols = [c for c in row if c not in pk_cols]
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols) if update_cols else ""

        sql = f"""
            INSERT INTO {table} ({', '.join(row.keys())})
            VALUES ({', '.join(['?' for _ in row])})
            ON CONFLICT ({pk_list}) DO UPDATE SET
            {', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)}
        """

        self.conn.execute(sql, list(row.values()))
        return True


class ColumnPruner:
    """列裁剪器 - 根据查询需求裁剪非必要列."""

    def __init__(self):
        self._column_usage: Dict[str, Set[str]] = defaultdict(set)

    def register_query(self, table: str, columns: List[str]):
        """注册查询使用的列."""
        self._column_usage[table].update(columns)

    def get_required_columns(self, table: str) -> Set[str]:
        """获取表的必要列."""
        return self._column_usage.get(table, set())

    def prune_row(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """裁剪行数据."""
        required = self.get_required_columns(table)
        if not required:
            return row
        return {k: v for k, v in row.items() if k in self._column_usage[table]}


class PreparedStatementCache:
    """预编译语句缓存."""

    def __init__(self, max_size: int = 100):
        self._cache: Dict[str, Any] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def get_or_create(self, key: str, creator: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if len(self._cache) >= self._max_size:
                # 简单 LRU: 删除最旧的
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            stmt = creator()
            self._cache[key] = stmt
            return stmt

    def clear(self):
        with self._lock:
            self._cache.clear()


# 全局实例
_column_pruner = ColumnPruner()
_stmt_cache = PreparedStatementCache()


def get_column_pruner() -> ColumnPruner:
    return _column_pruner


def get_statement_cache() -> PreparedStatementCache:
    return _stmt_cache