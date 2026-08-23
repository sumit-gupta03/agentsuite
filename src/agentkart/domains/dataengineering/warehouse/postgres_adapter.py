"""PostgreSQL adapter -- ``pip install "agent[postgres]"``."""

from __future__ import annotations

import os
import time
from typing import Any

from agentkart.core.errors import ConfigError
from agentkart.core.types import Column, QueryResult

from ..errors import WarehouseError
from .base import TableRef, Warehouse

try:  # pragma: no cover - exercised only where psycopg is installed
    import psycopg
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        'psycopg is not installed. Install it with: pip install "agent[postgres]"'
    ) from exc


class PostgresWarehouse(Warehouse):
    dialect = "postgres"
    quote_char = '"'

    def __init__(self, dsn: str | None = None, **kwargs: Any) -> None:
        self.dsn = dsn or os.environ.get("DE_POSTGRES_DSN") or os.environ.get("DATABASE_URL")
        if not self.dsn:
            raise ConfigError(
                "no Postgres DSN. Pass dsn=..., or set DE_POSTGRES_DSN / DATABASE_URL. "
                "Do not hardcode credentials in source."
            )
        self.default_schema = kwargs.pop("schema", "public")
        try:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(f"cannot connect to Postgres: {exc}") from exc

    @property
    def name(self) -> str:
        return "postgres"

    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        started = time.perf_counter()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(sql)
                return self._collect(cursor, max_rows, started)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc

    def list_schemas(self) -> list[str]:
        result = self.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
            "ORDER BY schema_name",
            max_rows=1000,
        )
        return [str(row[0]) for row in result.rows]

    def list_tables(self, schema: str | None = None) -> list[str]:
        target = schema or self.default_schema
        self._validate_identifier(target, "schema name")
        result = self.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{target}' ORDER BY table_name",
            max_rows=10_000,
        )
        return [str(row[0]) for row in result.rows]

    def describe_table(self, table: str) -> list[Column]:
        ref = TableRef.parse(table)
        schema = ref.schema or self.default_schema
        self._validate_identifier(ref.table, "table name")
        self._validate_identifier(schema, "schema name")
        result = self.execute(
            "SELECT column_name, data_type, is_nullable, col_description("
            f"'{schema}.{ref.table}'::regclass, ordinal_position) "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{ref.table}' "
            "ORDER BY ordinal_position",
            max_rows=5000,
        )
        if not result.rows:
            raise WarehouseError(f"table {table!r} does not exist in schema {schema!r}")
        return [
            Column(
                name=str(r[0]),
                type=str(r[1]),
                nullable=str(r[2]).upper() == "YES",
                comment=str(r[3]) if r[3] else None,
            )
            for r in result.rows
        ]

    def explain(self, sql: str) -> str:
        result = self.execute(f"EXPLAIN (FORMAT TEXT) {sql}", max_rows=500)
        return "\n".join(str(row[0]) for row in result.rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            pass


__all__ = ["PostgresWarehouse"]
