"""SQLite adapter -- the zero-dependency default.

Nothing here needs a driver install, which makes it the adapter the test suite
and the ``examples/`` scripts run against. It is a real adapter, not a mock:
the same guardrails, profiling and agent loop run over it unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from agentsuite.core.types import Column, QueryResult

from ..errors import WarehouseError
from .base import TableRef, Warehouse


class SQLiteWarehouse(Warehouse):
    dialect = "sqlite"
    quote_char = '"'

    def __init__(self, database: str = ":memory:", dsn: str | None = None, **_: Any) -> None:
        if dsn:
            database = dsn.split("://", 1)[-1] or ":memory:"
        self.database = database
        try:
            self._conn = sqlite3.connect(database)
        except sqlite3.Error as exc:
            raise WarehouseError(f"cannot open sqlite database {database!r}: {exc}") from exc
        self._conn.row_factory = None

    @property
    def name(self) -> str:
        return "sqlite"

    @property
    def connection(self) -> sqlite3.Connection:
        """Escape hatch for tests and fixtures that need to seed data."""
        return self._conn

    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        started = time.perf_counter()
        try:
            cursor = self._conn.execute(sql)
        except sqlite3.Error as exc:
            raise WarehouseError(str(exc)) from exc
        result = self._collect(cursor, max_rows, started)
        self._conn.commit()
        cursor.close()
        return result

    def list_schemas(self) -> list[str]:
        result = self.execute("PRAGMA database_list", max_rows=100)
        return [str(row[1]) for row in result.rows]

    def list_tables(self, schema: str | None = None) -> list[str]:
        result = self.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
            max_rows=10_000,
        )
        return [str(row[0]) for row in result.rows]

    def describe_table(self, table: str) -> list[Column]:
        ref = TableRef.parse(table)
        self._validate_identifier(ref.table, "table name")
        result = self.execute(f'PRAGMA table_info("{ref.table}")', max_rows=1000)
        if not result.rows:
            raise WarehouseError(f"table {table!r} does not exist")
        return [
            Column(name=str(row[1]), type=str(row[2]) or "UNKNOWN", nullable=not bool(row[3]))
            for row in result.rows
        ]

    def explain(self, sql: str) -> str:
        result = self.execute(f"EXPLAIN QUERY PLAN {sql}", max_rows=500)
        return result.to_markdown(max_rows=500)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - already closed
            pass


__all__ = ["SQLiteWarehouse"]
