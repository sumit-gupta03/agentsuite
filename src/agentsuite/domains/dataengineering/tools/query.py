"""Warehouse tools: the only path from a model turn to your database.

Every statement passes through :class:`~agentsuite.domains.dataengineering.policy.SqlPolicy`
before it reaches an adapter, and a destructive statement additionally has to
clear the session's confirmation callback. A refusal is returned as an error tool
result rather than raised, so the model can course-correct within the same run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentsuite.core.errors import ToolError
from agentsuite.core.tools import tool

from ..errors import WarehouseError
from ..policy import SqlPolicy, referenced_tables

if TYPE_CHECKING:
    from .. import WarehouseContext


@tool(
    name="list_schemas",
    description="List the schemas visible to the current warehouse credentials.",
    schema={"type": "object", "properties": {}, "required": []},
    requires=["warehouse"],
)
def list_schemas(context: WarehouseContext) -> str:
    schemas = context.db.list_schemas()
    return "\n".join(f"- {s}" for s in schemas) if schemas else "(no schemas visible)"


@tool(
    name="list_tables",
    description=(
        "List tables and views in a schema. Start here when you do not yet know the "
        "shape of the warehouse; guessing table names wastes turns."
    ),
    schema={
        "type": "object",
        "properties": {
            "schema": {
                "type": ["string", "null"],
                "description": "Schema to list. Null uses the connection's default schema.",
            }
        },
        "required": ["schema"],
    },
    requires=["warehouse"],
)
def list_tables(context: WarehouseContext, schema: str | None = None) -> str:
    tables = context.db.list_tables(schema)
    if not tables:
        return f"(no tables in {schema or 'the default schema'})"
    return "\n".join(f"- {t}" for t in tables)


@tool(
    name="describe_table",
    description=(
        "Return the column names, types and nullability for a table. Always describe a "
        "table before writing SQL against it -- inferred column names are the single "
        "largest source of failed queries."
    ),
    schema={
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "Table reference, optionally qualified: 'orders' or 'raw.orders'.",
            }
        },
        "required": ["table"],
    },
    requires=["warehouse"],
)
def describe_table(context: WarehouseContext, table: str) -> str:
    try:
        columns = context.db.describe_table(table)
    except WarehouseError as exc:
        raise ToolError(str(exc)) from exc
    lines = [
        f"### {table}",
        "",
        "| column | type | nullable | comment |",
        "| --- | --- | --- | --- |",
    ]
    for column in columns:
        comment = (column.comment or "").replace("|", r"\|")
        nullable = "yes" if column.nullable else "no"
        lines.append(f"| {column.name} | {column.type} | {nullable} | {comment} |")
    return "\n".join(lines)


@tool(
    name="run_query",
    description=(
        "Execute SQL against the warehouse and return the rows as a markdown table. "
        "Read statements are always permitted and receive an automatic LIMIT when they "
        "have none. Writes require a write-enabled session; destructive statements "
        "additionally require confirmation and will be refused otherwise. Submit one "
        "statement per call."
    ),
    schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SQL statement."},
            "purpose": {
                "type": "string",
                "description": (
                    "One sentence on why you are running this. Shown to the operator in "
                    "confirmation prompts and written to the audit log."
                ),
            },
        },
        "required": ["sql", "purpose"],
    },
    requires=["warehouse"],
)
def run_query(context: WarehouseContext, sql: str, purpose: str = "") -> str:
    warehouse = context.db
    policy = context.sql_policy
    dialect = warehouse.dialect or None

    verdict = policy.check(sql, dialect=dialect)

    if not verdict.allowed and not verdict.needs_confirmation:
        raise ToolError(verdict.reason)

    if verdict.needs_confirmation:
        tables = referenced_tables(sql, dialect=dialect)
        label = verdict.primary.label if verdict.primary else "statement"
        approved = context.confirm(
            action=f"{label} on {', '.join(tables) or 'the warehouse'}",
            detail=sql,
            purpose=purpose,
        )
        if not approved:
            raise ToolError(
                "refused: the operator declined this destructive statement. "
                "Propose a non-destructive alternative or explain why it is required."
            )

    to_run = verdict.rewritten or sql

    estimate = warehouse.estimate_cost(to_run)
    if estimate and context.cost_ceiling_exceeded(estimate):
        raise ToolError(
            f"refused: estimated scan exceeds this session's ceiling ({estimate.summary()}). "
            "Narrow the query with a partition filter or an aggregate."
        )

    try:
        result = warehouse.execute(to_run, max_rows=policy.max_rows)
    except WarehouseError as exc:
        raise ToolError(f"query failed: {exc}") from exc

    context.record(to_run, verdict.tier, purpose, kind="sql")

    header = f"_{result.row_count} row(s) in {result.elapsed_ms:.0f} ms_"
    if verdict.rewritten:
        header += f"\n_An automatic LIMIT {policy.max_rows} was added._"
    if estimate:
        header += f"\n_Estimated cost: {estimate.summary()}_"
    return f"{header}\n\n{result.to_markdown()}"


@tool(
    name="explain_query",
    description=(
        "Return the query plan without executing the statement. Use this before running "
        "anything expensive, and when diagnosing a slow query."
    ),
    schema={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "The SQL to explain."}},
        "required": ["sql"],
    },
    requires=["warehouse"],
)
def explain_query(context: WarehouseContext, sql: str) -> str:
    warehouse = context.db
    dialect = warehouse.dialect or None
    # Explaining is always read-only, whatever the session policy allows.
    verdict = SqlPolicy(write=False, auto_limit=False).check(sql, dialect=dialect)
    if not verdict.allowed:
        raise ToolError("only read statements can be explained: " + verdict.reason)
    try:
        plan = warehouse.explain(sql)
    except WarehouseError as exc:
        raise ToolError(f"could not explain: {exc}") from exc
    estimate = warehouse.estimate_cost(sql)
    suffix = f"\n\n_Estimated cost: {estimate.summary()}_" if estimate else ""
    return f"```\n{plan}\n```{suffix}"


@tool(
    name="estimate_query_cost",
    description=(
        "Estimate how much data a query would scan without running it. Supported on "
        "warehouses with a dry-run facility (BigQuery, Snowflake); returns a note "
        "elsewhere. Call this before any full-table scan."
    ),
    schema={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "The SQL to estimate."}},
        "required": ["sql"],
    },
    requires=["warehouse"],
)
def estimate_query_cost(context: WarehouseContext, sql: str) -> str:
    estimate = context.db.estimate_cost(sql)
    if estimate is None:
        return (
            f"{context.db.name} does not support pre-flight cost estimation. "
            "Use explain_query and check the plan for full scans instead."
        )
    return estimate.summary()
