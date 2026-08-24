"""Reconciliation: proving two tables agree, and locating where they do not.

The queries here are the ones people write by hand every time and get subtly
wrong -- ``<>`` instead of ``IS DISTINCT FROM``, counts without distinct-key
counts, float sums that never tie out. Getting them right once and running them
from a tool is most of what a reconciliation agent is for.

All comparisons run inside a single warehouse. Reconciling *across* systems
(Snowflake against Postgres) means landing one side first; the
``data-reconciliation`` skill covers how.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentsuite.core.errors import ToolError
from agentsuite.core.tools import tool

from ..errors import WarehouseError
from ..warehouse.base import TableRef

if TYPE_CHECKING:
    from .. import WarehouseContext

#: Truncating a timestamp to a day is not portable. ``CAST(x AS DATE)`` silently
#: yields an integer year on SQLite, which collapses every day into one bucket
#: and reports a real discrepancy as "no difference" -- so it is mapped per
#: dialect rather than assumed.
_DAY_EXPRESSION: dict[str, str] = {
    "sqlite": "DATE({column})",
    "bigquery": "DATE({column})",
    "duckdb": "CAST({column} AS DATE)",
    "postgres": "CAST({column} AS DATE)",
    "snowflake": "CAST({column} AS DATE)",
}
_DEFAULT_DAY_EXPRESSION = "CAST({column} AS DATE)"


def day_expression(column: str, dialect: str) -> str:
    """A day bucket for ``column`` that is correct on ``dialect``."""
    template = _DAY_EXPRESSION.get(dialect, _DEFAULT_DAY_EXPRESSION)
    return template.format(column=column)


@tool(
    name="compare_tables",
    description=(
        "Reconcile two tables on a key: row counts, distinct key counts, keys "
        "present in one side only, and (optionally) a checksum of a numeric column. "
        "This is the first thing to run when asked whether a copy, a migration or a "
        "rebuild matches its source. Reports where they differ, not just that they do."
    ),
    schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "The table of record, e.g. 'raw.orders'."},
            "target": {"type": "string", "description": "The table being checked."},
            "key_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns that together identify a row on both sides.",
            },
            "sum_column": {
                "type": ["string", "null"],
                "description": (
                    "Optional numeric column to checksum. Use an integer column "
                    "(minor units) -- float sums do not reconcile."
                ),
            },
        },
        "required": ["source", "target", "key_columns", "sum_column"],
    },
    requires=["warehouse"],
)
def compare_tables(
    context: WarehouseContext,
    source: str,
    target: str,
    key_columns: list[str],
    sum_column: str | None = None,
) -> str:
    warehouse = context.db
    quote = warehouse.quote_char

    if not key_columns:
        raise ToolError("key_columns must name at least one column")

    _require_columns(context, source, key_columns, sum_column)
    _require_columns(context, target, key_columns, sum_column)

    left = TableRef.parse(source).qualified(quote)
    right = TableRef.parse(target).qualified(quote)
    keys = [f"{quote}{c}{quote}" for c in key_columns]
    joined = " AND ".join(f"s.{k} = t.{k}" for k in keys)

    lines = [f"### Reconciling `{target}` against `{source}`", ""]
    counts: dict[str, int] = {}

    for label, table in (("source", left), ("target", right)):
        row = _one(
            context,
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT {_concat(keys, warehouse.dialect)}) AS k "
            f"FROM {table}",
        )
        counts[f"{label}_rows"] = int(row[0] or 0)
        counts[f"{label}_keys"] = int(row[1] or 0)

    lines += [
        "| side | rows | distinct keys |",
        "| --- | --- | --- |",
        f"| source | {counts['source_rows']:,} | {counts['source_keys']:,} |",
        f"| target | {counts['target_rows']:,} | {counts['target_keys']:,} |",
        "",
    ]

    findings: list[str] = []
    if counts["source_rows"] != counts["target_rows"]:
        delta = counts["target_rows"] - counts["source_rows"]
        findings.append(f"**Row counts differ by {delta:+,}.**")
    for side in ("source", "target"):
        if counts[f"{side}_rows"] != counts[f"{side}_keys"]:
            duplicated = counts[f"{side}_rows"] - counts[f"{side}_keys"]
            findings.append(
                f"**The {side} is not unique on the key** -- {duplicated:,} duplicate row(s). "
                "Fix that before trusting any other number here."
            )

    missing = _scalar(
        context,
        f"SELECT COUNT(*) FROM {left} s LEFT JOIN {right} t ON {joined} "
        f"WHERE t.{keys[0]} IS NULL",
    )
    extra = _scalar(
        context,
        f"SELECT COUNT(*) FROM {right} t LEFT JOIN {left} s ON {joined} "
        f"WHERE s.{keys[0]} IS NULL",
    )
    lines += [
        f"- keys in source but not target: **{missing:,}**",
        f"- keys in target but not source: **{extra:,}**",
    ]
    if missing:
        findings.append(f"**{missing:,} key(s) are missing from the target.**")
    if extra:
        findings.append(f"**{extra:,} key(s) in the target do not exist in the source.**")

    if sum_column:
        column = f"{quote}{sum_column}{quote}"
        source_sum = _scalar(context, f"SELECT COALESCE(SUM({column}), 0) FROM {left}")
        target_sum = _scalar(context, f"SELECT COALESCE(SUM({column}), 0) FROM {right}")
        lines += [
            "",
            f"- `SUM({sum_column})` source: **{source_sum:,}**",
            f"- `SUM({sum_column})` target: **{target_sum:,}**",
        ]
        if source_sum != target_sum:
            findings.append(
                f"**`SUM({sum_column})` differs by {target_sum - source_sum:+,}.** "
                "Counts can agree while values do not; this is the check that catches it."
            )

    if missing or extra:
        sample = _rows(
            context,
            f"SELECT {', '.join(f's.{k}' for k in keys)} FROM {left} s "
            f"LEFT JOIN {right} t ON {joined} WHERE t.{keys[0]} IS NULL",
            limit=10,
        )
        if sample:
            lines += ["", "**Sample keys missing from the target:**", ""]
            lines += [f"- `{', '.join(str(v) for v in row)}`" for row in sample]

    context.record(f"compare_tables({source}, {target})", "read", "reconciliation", kind="sql")

    lines += ["", "---", ""]
    lines += findings if findings else ["The two tables agree on every check run here."]
    return "\n".join(lines)


@tool(
    name="compare_by_period",
    description=(
        "Compare row counts between two tables bucketed by a date or timestamp "
        "column, and report only the periods that differ. Use this after "
        "compare_tables shows a mismatch -- it localises the discrepancy to specific "
        "days, which usually names the incident that caused it."
    ),
    schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "The table of record."},
            "target": {"type": "string", "description": "The table being checked."},
            "date_column": {
                "type": "string",
                "description": "Date or timestamp column present on both sides.",
            },
            "limit": {"type": "integer", "description": "How many differing periods to show."},
        },
        "required": ["source", "target", "date_column", "limit"],
    },
    requires=["warehouse"],
)
def compare_by_period(
    context: WarehouseContext,
    source: str,
    target: str,
    date_column: str,
    limit: int = 30,
) -> str:
    warehouse = context.db
    quote = warehouse.quote_char
    limit = max(1, min(int(limit or 30), 200))

    _require_columns(context, source, [date_column], None)
    _require_columns(context, target, [date_column], None)

    left = TableRef.parse(source).qualified(quote)
    right = TableRef.parse(target).qualified(quote)
    column = f"{quote}{date_column}{quote}"
    day = day_expression(column, warehouse.dialect)

    sql = (
        f"SELECT COALESCE(s.d, t.d) AS period, "
        f"COALESCE(s.n, 0) AS source_rows, COALESCE(t.n, 0) AS target_rows, "
        f"COALESCE(t.n, 0) - COALESCE(s.n, 0) AS delta "
        f"FROM (SELECT {day} AS d, COUNT(*) AS n FROM {left} GROUP BY 1) s "
        f"FULL OUTER JOIN (SELECT {day} AS d, COUNT(*) AS n FROM {right} GROUP BY 1) t "
        f"ON s.d = t.d "
        f"WHERE COALESCE(s.n, 0) <> COALESCE(t.n, 0) "
        f"ORDER BY period LIMIT {limit}"
    )

    try:
        result = warehouse.execute(sql, max_rows=limit)
    except WarehouseError as exc:
        if "FULL OUTER" in str(exc).upper() or "full outer" in str(exc).lower():
            raise ToolError(
                f"{warehouse.name} does not support FULL OUTER JOIN here. Compare each "
                "side with a grouped run_query and diff the periods yourself."
            ) from exc
        raise ToolError(f"period comparison failed: {exc}") from exc

    context.record(
        f"compare_by_period({source}, {target}, {date_column})",
        "read",
        "reconciliation",
        kind="sql",
    )

    if not result.rows:
        return (
            f"Row counts agree for every period of `{date_column}`.\n\n"
            "_Counts agreeing does not mean the values agree -- duplicated rows offset "
            "by dropped rows produce an identical count. Use compare_tables with a "
            "sum_column._"
        )

    lines = [
        f"### Periods where `{source}` and `{target}` disagree",
        "",
        "| period | source | target | delta |",
        "| --- | --- | --- | --- |",
    ]
    for period, source_rows, target_rows, delta in result.rows:
        lines.append(
            f"| {period} | {int(source_rows):,} | {int(target_rows):,} | {int(delta):+,} |"
        )
    if result.truncated or len(result.rows) >= limit:
        lines.append(f"\n_Capped at {limit} periods; there may be more._")
    return "\n".join(lines)


# -- helpers ---------------------------------------------------------------


def _require_columns(
    context: WarehouseContext, table: str, columns: list[str], extra: str | None
) -> None:
    try:
        present = {c.name for c in context.db.describe_table(table)}
    except WarehouseError as exc:
        raise ToolError(str(exc)) from exc
    wanted = {*columns, *([extra] if extra else [])}
    missing = wanted - present
    if missing:
        raise ToolError(
            f"{table!r} has no column(s): {', '.join(sorted(missing))}. "
            f"Columns present: {', '.join(sorted(present))}"
        )


def _concat(keys: list[str], dialect: str) -> str:
    """A single expression identifying a composite key, for DISTINCT counting."""
    if len(keys) == 1:
        return keys[0]
    cast = [f"CAST({k} AS VARCHAR)" for k in keys]
    # A separator matters: ('a','bc') and ('ab','c') must not collide.
    joined = " || '␟' || ".join(cast)
    return joined


def _one(context: WarehouseContext, sql: str) -> tuple[Any, ...]:
    try:
        result = context.db.execute(sql, max_rows=1)
    except WarehouseError as exc:
        raise ToolError(f"reconciliation query failed: {exc}") from exc
    if not result.rows:
        raise ToolError("reconciliation query returned no rows")
    return result.rows[0]


def _scalar(context: WarehouseContext, sql: str) -> int:
    value = _one(context, sql)[0]
    return int(value or 0)


def _rows(context: WarehouseContext, sql: str, *, limit: int) -> list[tuple[Any, ...]]:
    try:
        return context.db.execute(f"{sql} LIMIT {limit}", max_rows=limit).rows
    except WarehouseError:
        return []
