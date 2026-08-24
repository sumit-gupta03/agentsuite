"""Snowflake adapter -- ``pip install "agent[snowflake]"``.

Credentials are read from the environment or a named connection, never taken as
positional constructor arguments -- that keeps keys out of notebooks and
tracebacks.
"""

from __future__ import annotations

import os
import time
from typing import Any

from agentsuite.core.errors import ConfigError
from agentsuite.core.types import Column, QueryResult

from ..errors import WarehouseError
from .base import CostEstimate, TableRef, Warehouse

try:  # pragma: no cover - exercised only where the connector is installed
    import snowflake.connector as sf
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "snowflake-connector-python is not installed. "
        'Install it with: pip install "agent[snowflake]"'
    ) from exc


_ENV = {
    "account": "SNOWFLAKE_ACCOUNT",
    "user": "SNOWFLAKE_USER",
    "password": "SNOWFLAKE_PASSWORD",
    "role": "SNOWFLAKE_ROLE",
    "warehouse": "SNOWFLAKE_WAREHOUSE",
    "database": "SNOWFLAKE_DATABASE",
    "schema": "SNOWFLAKE_SCHEMA",
    "authenticator": "SNOWFLAKE_AUTHENTICATOR",
    "private_key_file": "SNOWFLAKE_PRIVATE_KEY_FILE",
}


class SnowflakeWarehouse(Warehouse):
    dialect = "snowflake"
    quote_char = '"'

    def __init__(self, **kwargs: Any) -> None:
        params = {key: kwargs.get(key) or os.environ.get(env) for key, env in _ENV.items()}
        params = {k: v for k, v in params.items() if v}
        if not params.get("account"):
            raise ConfigError(
                "no Snowflake account. Set SNOWFLAKE_ACCOUNT (and SNOWFLAKE_USER plus one of "
                "SNOWFLAKE_PASSWORD / SNOWFLAKE_PRIVATE_KEY_FILE / SNOWFLAKE_AUTHENTICATOR)."
            )
        self.default_schema: str = str(params.get("schema") or "PUBLIC")
        self.default_database = params.get("database")
        try:
            self._conn = sf.connect(**params)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(f"cannot connect to Snowflake: {exc}") from exc

    @property
    def name(self) -> str:
        return "snowflake"

    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        started = time.perf_counter()
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            return self._collect(cursor, max_rows, started)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc
        finally:
            cursor.close()

    def list_schemas(self) -> list[str]:
        result = self.execute("SHOW SCHEMAS", max_rows=10_000)
        idx = result.columns.index("name") if "name" in result.columns else 1
        return [str(row[idx]) for row in result.rows]

    def list_tables(self, schema: str | None = None) -> list[str]:
        target = schema or self.default_schema
        self._validate_identifier(target, "schema name")
        result = self.execute(f"SHOW TABLES IN SCHEMA {target}", max_rows=10_000)
        idx = result.columns.index("name") if "name" in result.columns else 1
        return [str(row[idx]) for row in result.rows]

    def describe_table(self, table: str) -> list[Column]:
        ref = TableRef.parse(table)
        for part in (ref.table, ref.schema, ref.database):
            if part:
                self._validate_identifier(part, "identifier")
        result = self.execute(f"DESCRIBE TABLE {ref}", max_rows=5000)
        if not result.rows:
            raise WarehouseError(f"table {table!r} does not exist")
        return [
            Column(
                name=str(r[0]),
                type=str(r[1]),
                nullable=str(r[3]).upper() == "Y" if len(r) > 3 else True,
                comment=str(r[9]) if len(r) > 9 and r[9] else None,
            )
            for r in result.rows
        ]

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """Compile the statement without running it and read the plan's estimate."""
        try:
            result = self.execute(f"EXPLAIN USING TEXT {sql}", max_rows=200)
        except WarehouseError:
            return None
        text = "\n".join(str(row[0]) for row in result.rows)
        return CostEstimate(note=f"Snowflake plan:\n{text[:2000]}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            pass


__all__ = ["SnowflakeWarehouse"]
