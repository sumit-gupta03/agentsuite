"""SQL classification -- the data domain's half of the permission layer.

:class:`~agentsuite.core.policy.Policy` owns the decision (tiers, the write gate, the
confirmation gate, fail-closed). This module owns only the question it cannot
answer generically: *what kind of statement is this?*

Skills describe what to do; nothing here can be widened by a skill file. A
malicious or merely careless skill pack can ask for a ``DROP TABLE`` and still be
refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from agentsuite.core.policy import Action, Policy

READ_EXPRESSIONS = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Describe,
    exp.Show,
    exp.Pragma,
)

WRITE_EXPRESSIONS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Alter,
    exp.Copy,
)

# Built with getattr so a sqlglot version that renames or drops a node degrades
# to the keyword matcher rather than failing to import. The keyword matcher also
# classifies these as destructive, so coverage never depends on this list alone.
DESTRUCTIVE_EXPRESSIONS = tuple(
    node
    for node in (
        getattr(exp, name, None) for name in ("Drop", "Grant", "Revoke", "TruncateTable")
    )
    if node is not None
)

#: Human-readable names for statement kinds, so refusals read like English.
_KIND_LABELS = {"TRUNCATETABLE": "TRUNCATE", "TRUNCATE_TABLE": "TRUNCATE"}

_DESTRUCTIVE_KEYWORDS = re.compile(r"^\s*(drop|truncate|revoke|grant)\b", re.IGNORECASE)
_WRITE_KEYWORDS = re.compile(
    r"^\s*(insert|update|delete|merge|create|alter|replace|copy|vacuum|call)\b", re.IGNORECASE
)
_READ_KEYWORDS = re.compile(
    r"^\s*(select|with|show|describe|desc|explain|pragma)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class SqlPolicy(Policy):
    """Permission policy for SQL statements."""

    #: Refuse several statements submitted in one call.
    allow_multi_statement: bool = False
    #: Row cap applied to results and to injected LIMIT clauses.
    max_rows: int = 1000
    #: Add a LIMIT to bare SELECTs so nobody scans a fact table by accident.
    auto_limit: bool = True
    #: sqlglot dialect, set from the connected warehouse.
    dialect: str | None = None
    #: Refuse a query whose dry-run estimate exceeds this many gigabytes.
    max_scan_gb: float | None = None

    def describe(self) -> str:
        base = super().describe()
        return f"{base}; results capped at {self.max_rows} rows"

    # -- classification -----------------------------------------------------

    def classify(self, request: str, **context: Any) -> list[Action]:
        dialect = context.get("dialect", self.dialect)
        try:
            parsed = sqlglot.parse(request, read=dialect)
        except Exception:  # noqa: BLE001 - any parse failure falls back to keywords
            parsed = None

        if not parsed or any(node is None for node in parsed):
            return [_by_keyword(chunk) for chunk in _split_raw(request)]

        return [_from_node(node, dialect=dialect) for node in parsed if node is not None]

    def validate(self, actions: list[Action]) -> str | None:
        if len(actions) > 1 and not self.allow_multi_statement:
            return (
                f"refused: {len(actions)} statements in one call. Submit them one at a time "
                "so each can be reviewed and rolled back independently."
            )
        return None

    def rewrite(self, request: str, actions: list[Action], **context: Any) -> str | None:
        if not self.auto_limit:
            return None
        return apply_limit(
            actions[0].detail, self.max_rows, dialect=context.get("dialect", self.dialect)
        )

    def exceeds_scan_ceiling(self, gigabytes: float | None) -> bool:
        if self.max_scan_gb is None or gigabytes is None:
            return False
        return gigabytes > self.max_scan_gb


def _from_node(node: exp.Expression, *, dialect: str | None) -> Action:
    rendered = node.sql(dialect=dialect)
    raw_kind = type(node).__name__.upper()
    kind = _KIND_LABELS.get(raw_kind, raw_kind)

    if DESTRUCTIVE_EXPRESSIONS and isinstance(node, DESTRUCTIVE_EXPRESSIONS):
        return Action("sql", rendered, "destructive", kind, f"{kind} permanently removes data")

    if isinstance(node, exp.Command):
        # sqlglot parks TRUNCATE/VACUUM/etc. here on some dialects.
        return _by_keyword(rendered)

    if isinstance(node, exp.Delete):
        if not node.args.get("where"):
            return Action("sql", rendered, "destructive", kind, "DELETE without a WHERE clause")
        return Action("sql", rendered, "write", kind)

    if isinstance(node, exp.Update):
        if not node.args.get("where"):
            return Action("sql", rendered, "destructive", kind, "UPDATE without a WHERE clause")
        return Action("sql", rendered, "write", kind)

    if isinstance(node, WRITE_EXPRESSIONS):
        return Action("sql", rendered, "write", kind)

    if isinstance(node, (*READ_EXPRESSIONS, exp.With, exp.Subquery)):
        return Action("sql", rendered, "read", kind)

    # Unknown node type: fail closed.
    return Action("sql", rendered, "destructive", kind, f"unrecognised statement type {kind}")


def _by_keyword(sql: str) -> Action:
    stripped = sql.strip().rstrip(";")
    if not stripped:
        return Action("sql", stripped, "read", "EMPTY")
    if _DESTRUCTIVE_KEYWORDS.match(stripped):
        head = stripped.split()[0].upper()
        return Action("sql", stripped, "destructive", head, f"{head} is destructive")
    if _WRITE_KEYWORDS.match(stripped):
        return Action("sql", stripped, "write", stripped.split()[0].upper())
    if _READ_KEYWORDS.match(stripped):
        return Action("sql", stripped, "read", stripped.split()[0].upper())
    return Action("sql", stripped, "destructive", "UNKNOWN", "statement could not be classified")


def _split_raw(sql: str) -> list[str]:
    return [chunk.strip() for chunk in sql.split(";") if chunk.strip()]


def apply_limit(sql: str, max_rows: int, *, dialect: str | None = None) -> str | None:
    """Add a LIMIT to a bare SELECT.

    Returns ``None`` when nothing needed changing -- the statement already has a
    LIMIT, is a single-row aggregate, or could not be parsed.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - unparseable SQL is left alone
        return None
    if tree is None or not isinstance(tree, (exp.Select, exp.Union)):
        return None
    if tree.args.get("limit") is not None:
        return None
    if isinstance(tree, exp.Select) and _is_scalar_aggregate(tree):
        return None
    try:
        return tree.limit(max_rows).sql(dialect=dialect)
    except Exception:  # noqa: BLE001 - dialect quirks
        return None


def _is_scalar_aggregate(select: exp.Select) -> bool:
    """A single-row aggregate needs no LIMIT and reads better without one."""
    if select.args.get("group"):
        return False
    return any(isinstance(node, exp.AggFunc) for node in select.expressions)


def referenced_tables(sql: str, *, dialect: str | None = None) -> list[str]:
    """Best-effort list of tables a statement touches, for logging and prompts."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return []
    if tree is None:
        return []
    names: list[str] = []
    for table in tree.find_all(exp.Table):
        rendered = table.sql(dialect=dialect)
        if rendered not in names:
            names.append(rendered)
    return names


__all__ = ["SqlPolicy", "apply_limit", "referenced_tables"]
