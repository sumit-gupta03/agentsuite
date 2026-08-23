"""DuckDB adapter -- ``pip install "agent[duckdb]"``."""

from __future__ import annotations

import time
from typing import Any

from agentkart.core.types import Column, QueryResult

from ..errors import WarehouseError
from .base import TableRef, Warehouse

try:  # pragma: no cover - exercised only where duckdb is installed
    import duckdb
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        'duckdb is not installed. Install it with: pip install "agent[duckdb]"'
    ) from exc


class DuckDBWarehouse(Warehouse):
    dialect = "duckdb"
    quote_char = '"'

    def __init__(
        self,
        database: str = ":memory:",
        dsn: str | None = None,
        read_only: bool = False,
        **_: Any,
    ) -> None:
        if dsn:
            database = dsn.split("://", 1)[-1] or ":memory:"
        self.database = database
        try:
            self._conn = duckdb.connect(database, read_only=read_only)
        except Exception as exc:  # noqa: BLE001 - duckdb raises a wide range
            raise WarehouseError(f"cannot open duckdb database {database!r}: {exc}") from exc

    @property
    def name(self) -> str:
        return "duckdb"

    @property
    def connection(self) -> Any:
        return self._conn

    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        started = time.perf_counter()
        try:
            cursor = self._conn.execute(sql)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc
        return self._collect(cursor, max_rows, started)

    def list_schemas(self) -> list[str]:
        result = self.execute(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            max_rows=1000,
        )
        return [str(row[0]) for row in result.rows]

    def list_tables(self, schema: str | None = None) -> list[str]:
        if schema:
            self._validate_identifier(schema, "schema name")
            sql = (
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{schema}' ORDER BY table_name"
            )
        else:
            sql = (
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema','pg_catalog') "
                "ORDER BY table_name"
            )
        return [str(row[0]) for row in self.execute(sql, max_rows=10_000).rows]

    def describe_table(self, table: str) -> list[Column]:
        ref = TableRef.parse(table)
        self._validate_identifier(ref.table, "table name")
        where = f"table_name = '{ref.table}'"
        if ref.schema:
            self._validate_identifier(ref.schema, "schema name")
            where += f" AND table_schema = '{ref.schema}'"
        result = self.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            f"WHERE {where} ORDER BY ordinal_position",
            max_rows=5000,
        )
        if not result.rows:
            raise WarehouseError(f"table {table!r} does not exist")
        return [
            Column(name=str(r[0]), type=str(r[1]), nullable=str(r[2]).upper() == "YES")
            for r in result.rows
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            pass


__all__ = ["DuckDBWarehouse"]
