"""Profiling tools.

These are the tools that make the agent useful rather than merely conversational.
Each one is a single well-shaped query the model would otherwise have to invent,
get subtly wrong, and iterate on across three turns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentkart.core.errors import ToolError
from agentkart.core.tools import tool

from ..errors import WarehouseError
from ..warehouse.base import TableRef

if TYPE_CHECKING:
    from .. import WarehouseContext

#: Types we can compute numeric statistics over, matched case-insensitively by prefix.
_NUMERIC_PREFIXES = ("int", "float", "double", "decimal", "numeric", "real", "bigint", "smallint")


def _is_numeric(type_name: str) -> bool:
    lowered = type_name.lower()
    return any(lowered.startswith(prefix) for prefix in _NUMERIC_PREFIXES)


@tool(
    name="profile_table",
    description=(
        "Profile a table: row count, and per column the null rate, distinct count and "
        "(for numeric columns) min/max/mean. This is the right first move when asked to "
        "assess data quality, investigate a suspect table, or plan a migration."
    ),
    schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table to profile, e.g. 'raw.orders'."},
            "columns": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Specific columns to profile. Null profiles every column.",
            },
        },
        "required": ["table", "columns"],
    },
    requires=["warehouse"],
)
def profile_table(context: WarehouseContext, table: str, columns: list[str] | None = None) -> str:
    warehouse = context.db
    quote = warehouse.quote_char
    ref = TableRef.parse(table)
    qualified = ref.qualified(quote)

    try:
        schema = warehouse.describe_table(table)
    except WarehouseError as exc:
        raise ToolError(str(exc)) from exc

    # Name the missing columns before complaining about an empty selection --
    # "no column 'amont'" is actionable; "nothing matched" is not.
    if columns:
        missing = set(columns) - {c.name for c in schema}
        if missing:
            raise ToolError(f"{table!r} has no column(s): {', '.join(sorted(missing))}")

    selected = [c for c in schema if columns is None or c.name in set(columns)]
    if not selected:
        raise ToolError(f"no columns to profile on {table!r}")

    total = warehouse.row_count(table)
    if total == 0:
        return f"### {table}\n\n**0 rows** -- nothing to profile."

    lines = [
        f"### {table}",
        "",
        f"**{total:,} rows**",
        "",
        "| column | type | nulls | null % | distinct | min | max | mean |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for column in selected:
        col = f"{quote}{column.name}{quote}"
        parts = [
            f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS null_count",
            f"COUNT(DISTINCT {col}) AS distinct_count",
        ]
        numeric = _is_numeric(column.type)
        if numeric:
            parts += [
                f"MIN({col}) AS min_value",
                f"MAX({col}) AS max_value",
                f"AVG(CAST({col} AS DOUBLE PRECISION)) AS mean_value"
                if warehouse.dialect not in {"sqlite", "bigquery"}
                else f"AVG({col}) AS mean_value",
            ]

        sql = f"SELECT {', '.join(parts)} FROM {qualified}"
        try:
            row = warehouse.execute(sql, max_rows=1).rows[0]
        except (WarehouseError, IndexError):
            lines.append(f"| {column.name} | {column.type} | ? | ? | ? | | | |")
            continue

        nulls = int(row[0] or 0)
        distinct = int(row[1] or 0)
        pct = f"{nulls / total * 100:.1f}%"
        min_v = _fmt(row[2]) if numeric and len(row) > 2 else ""
        max_v = _fmt(row[3]) if numeric and len(row) > 3 else ""
        mean_v = _fmt(row[4]) if numeric and len(row) > 4 else ""
        lines.append(
            f"| {column.name} | {column.type} | {nulls:,} | {pct} | {distinct:,} "
            f"| {min_v} | {max_v} | {mean_v} |"
        )

    context.record(f"profile_table({table})", "read", "profiling", kind="sql")
    return "\n".join(lines)


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


@tool(
    name="column_distribution",
    description=(
        "Return the most frequent values in a column with their counts and share of "
        "rows. Use this to spot sentinel values ('N/A', -1, epoch dates), skew that "
        "will wreck a join, and unexpected cardinality."
    ),
    schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table to sample."},
            "column": {"type": "string", "description": "Column to bucket by."},
            "top_n": {"type": "integer", "description": "How many values to return (1-100)."},
        },
        "required": ["table", "column", "top_n"],
    },
    requires=["warehouse"],
)
def column_distribution(context: WarehouseContext, table: str, column: str, top_n: int = 20) -> str:
    warehouse = context.db
    quote = warehouse.quote_char
    top_n = max(1, min(int(top_n), 100))

    known = {c.name for c in warehouse.describe_table(table)}
    if column not in known:
        raise ToolError(f"{table!r} has no column {column!r}. Columns: {', '.join(sorted(known))}")

    ref = TableRef.parse(table)
    col = f"{quote}{column}{quote}"
    sql = (
        f"SELECT {col} AS value, COUNT(*) AS n FROM {ref.qualified(quote)} "
        f"GROUP BY {col} ORDER BY n DESC LIMIT {top_n}"
    )
    try:
        result = warehouse.execute(sql, max_rows=top_n)
    except WarehouseError as exc:
        raise ToolError(str(exc)) from exc

    total = warehouse.row_count(table) or 1
    lines = [
        f"### {table}.{column} -- top {len(result.rows)} values",
        "",
        "| value | count | share |",
        "| --- | --- | --- |",
    ]
    for value, count in result.rows:
        label = "NULL" if value is None else str(value).replace("|", "\\|")
        lines.append(f"| {label} | {int(count):,} | {int(count) / total * 100:.1f}% |")
    return "\n".join(lines)


@tool(
    name="check_freshness",
    description=(
        "Report the newest and oldest value of a timestamp column and how stale the "
        "table is. Use this before trusting any table in an incremental pipeline, and "
        "when diagnosing a pipeline that 'ran fine' but produced nothing."
    ),
    schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table to check."},
            "timestamp_column": {
                "type": "string",
                "description": "The column carrying load or event time.",
            },
        },
        "required": ["table", "timestamp_column"],
    },
    requires=["warehouse"],
)
def check_freshness(context: WarehouseContext, table: str, timestamp_column: str) -> str:
    warehouse = context.db
    quote = warehouse.quote_char
    known = {c.name for c in warehouse.describe_table(table)}
    if timestamp_column not in known:
        raise ToolError(
            f"{table!r} has no column {timestamp_column!r}. Columns: {', '.join(sorted(known))}"
        )

    ref = TableRef.parse(table)
    col = f"{quote}{timestamp_column}{quote}"
    sql = (
        f"SELECT MIN({col}) AS oldest, MAX({col}) AS newest, COUNT(*) AS n, "
        f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls "
        f"FROM {ref.qualified(quote)}"
    )
    try:
        row = warehouse.execute(sql, max_rows=1).rows[0]
    except (WarehouseError, IndexError) as exc:
        raise ToolError(f"freshness check failed: {exc}") from exc

    oldest, newest, count, nulls = row
    lines = [
        f"### Freshness: {table}.{timestamp_column}",
        "",
        f"- newest: `{newest}`",
        f"- oldest: `{oldest}`",
        f"- rows: {int(count or 0):,}",
        f"- null timestamps: {int(nulls or 0):,}",
    ]
    if nulls:
        lines.append(
            "\n**Null timestamps present.** An incremental filter on this column will "
            "silently drop those rows on every run."
        )
    return "\n".join(lines)


@tool(
    name="find_duplicates",
    description=(
        "Count rows that share the same value for a candidate key, returning the worst "
        "offenders. Run this before declaring any column a primary key, and whenever a "
        "join inflates row counts."
    ),
    schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table to check."},
            "key_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The columns that should together be unique.",
            },
            "limit": {"type": "integer", "description": "How many duplicate keys to show."},
        },
        "required": ["table", "key_columns", "limit"],
    },
    requires=["warehouse"],
)
def find_duplicates(
    context: WarehouseContext, table: str, key_columns: list[str], limit: int = 10
) -> str:
    warehouse = context.db
    quote = warehouse.quote_char
    limit = max(1, min(int(limit), 100))
    if not key_columns:
        raise ToolError("key_columns must name at least one column")

    known = {c.name for c in warehouse.describe_table(table)}
    missing = set(key_columns) - known
    if missing:
        raise ToolError(f"{table!r} has no column(s): {', '.join(sorted(missing))}")

    ref = TableRef.parse(table)
    cols = ", ".join(f"{quote}{c}{quote}" for c in key_columns)
    sql = (
        f"SELECT {cols}, COUNT(*) AS n FROM {ref.qualified(quote)} "
        f"GROUP BY {cols} HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT {limit}"
    )
    try:
        result = warehouse.execute(sql, max_rows=limit)
    except WarehouseError as exc:
        raise ToolError(str(exc)) from exc

    if not result.rows:
        return f"`{', '.join(key_columns)}` is unique in {table} -- no duplicate keys found."

    header = "| " + " | ".join([*key_columns, "rows"]) + " |"
    divider = "| " + " | ".join("---" for _ in range(len(key_columns) + 1)) + " |"
    lines = [
        f"### Duplicate keys in {table}",
        "",
        f"**`{', '.join(key_columns)}` is NOT unique.**",
        "",
        header,
        divider,
    ]
    for row in result.rows:
        lines.append("| " + " | ".join("NULL" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)
