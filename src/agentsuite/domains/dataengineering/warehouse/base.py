"""The warehouse contract every adapter implements."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from agentsuite.core.types import Column, QueryResult

from ..errors import WarehouseError

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class TableRef:
    """A parsed ``[database.][schema.]table`` reference."""

    table: str
    schema: str | None = None
    database: str | None = None

    @classmethod
    def parse(cls, raw: str) -> TableRef:
        parts = [p.strip().strip('"').strip("`").strip("[]") for p in raw.split(".")]
        parts = [p for p in parts if p]
        if not parts:
            raise WarehouseError(f"empty table reference: {raw!r}")
        if len(parts) == 1:
            return cls(table=parts[0])
        if len(parts) == 2:
            return cls(schema=parts[0], table=parts[1])
        if len(parts) == 3:
            return cls(database=parts[0], schema=parts[1], table=parts[2])
        raise WarehouseError(f"table reference has too many parts: {raw!r}")

    def qualified(self, quote: str = '"') -> str:
        parts = [p for p in (self.database, self.schema, self.table) if p]
        return ".".join(f"{quote}{p}{quote}" for p in parts)

    def __str__(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.table) if p)


@dataclass
class CostEstimate:
    """A pre-flight estimate, when the warehouse can produce one."""

    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    currency_estimate: float | None = None
    note: str = ""

    def summary(self) -> str:
        bits = []
        if self.bytes_scanned is not None:
            bits.append(f"{self.bytes_scanned / 1e9:.2f} GB scanned")
        if self.rows_scanned is not None:
            bits.append(f"{self.rows_scanned:,} rows scanned")
        if self.currency_estimate is not None:
            bits.append(f"~${self.currency_estimate:.2f}")
        if self.note:
            bits.append(self.note)
        return "; ".join(bits) if bits else "no estimate available"


class Warehouse(ABC):
    """Abstract base for every warehouse adapter.

    Adapters are responsible for connectivity and metadata only. They must not
    implement policy -- :mod:`dataengineering.guardrails` decides what runs, and
    it runs *before* anything reaches an adapter.
    """

    #: sqlglot dialect name, used for parsing and LIMIT rewriting
    dialect: str = ""
    #: identifier quote character for this warehouse
    quote_char: str = '"'

    @property
    def name(self) -> str:
        return type(self).__name__.replace("Warehouse", "").lower()

    @abstractmethod
    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        """Run ``sql`` and return at most ``max_rows`` rows."""

    @abstractmethod
    def list_schemas(self) -> list[str]:
        """Return every schema visible to the current credentials."""

    @abstractmethod
    def list_tables(self, schema: str | None = None) -> list[str]:
        """Return table names in ``schema`` (or the default schema)."""

    @abstractmethod
    def describe_table(self, table: str) -> list[Column]:
        """Return the column list for ``table``."""

    def explain(self, sql: str) -> str:
        """Return the query plan as text. Adapters override where syntax differs."""
        result = self.execute(f"EXPLAIN {sql}", max_rows=500)
        return result.to_markdown(max_rows=500)

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """Estimate scan cost before running. ``None`` when unsupported."""
        return None

    def row_count(self, table: str) -> int:
        ref = TableRef.parse(table)
        result = self.execute(
            f"SELECT COUNT(*) AS n FROM {ref.qualified(self.quote_char)}", max_rows=1
        )
        return int(result.rows[0][0]) if result.rows else 0

    def close(self) -> None:  # noqa: B027 - adapters without a connection need no teardown
        """Release the connection. Safe to call more than once."""

    def describe_environment(self) -> list[str]:
        """Lines about this connection for the system prompt."""
        suffix = f" (dialect: {self.dialect})" if self.dialect else ""
        return [f"Warehouse: **{self.name}**{suffix}"]

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers for adapter implementations -------------------------------

    def _validate_identifier(self, value: str, kind: str = "identifier") -> str:
        """Reject identifiers that cannot be safely interpolated.

        Metadata queries interpolate schema and table names because most
        warehouses will not bind them as parameters. Everything that reaches
        such a query passes through here first.
        """
        if not _IDENTIFIER.match(value):
            raise WarehouseError(
                f"{kind} {value!r} contains characters that cannot be safely interpolated"
            )
        return value

    @staticmethod
    def _collect(cursor: Any, max_rows: int, started: float) -> QueryResult:
        """Shared DB-API result collection with truncation tracking."""
        columns = [d[0] for d in (cursor.description or [])]
        rows: list[tuple[Any, ...]] = []
        truncated = False
        if columns:
            fetched = cursor.fetchmany(max_rows + 1)
            if len(fetched) > max_rows:
                truncated = True
                fetched = fetched[:max_rows]
            rows = [tuple(r) for r in fetched]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )


__all__ = ["CostEstimate", "TableRef", "Warehouse"]
