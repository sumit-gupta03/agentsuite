"""Shared data structures that cross module boundaries.

These types are the contract between the four layers of the package:
skills -> agent -> model -> tools.  Keeping them here (rather than in whichever
module happens to construct them) is what lets the model backend stay ignorant
of tools, and the tool layer stay ignorant of the provider wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSONSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The outcome of executing a :class:`ToolCall`."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    """Token accounting for one model turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class ModelTurn:
    """One assistant turn, normalised across providers.

    ``assistant_message`` is the provider-native message dict.  The agent treats
    it as opaque and simply appends it to the transcript -- only the
    :class:`~agent.model.Model` implementation knows its shape.
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    assistant_message: dict[str, Any] = field(default_factory=dict)


@dataclass
class Column:
    """One column of a table, as reported by the warehouse."""

    name: str
    type: str
    nullable: bool = True
    comment: str | None = None


@dataclass
class QueryResult:
    """Rows returned by a warehouse query."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool = False
    elapsed_ms: float = 0.0

    def to_markdown(self, max_rows: int = 50, max_cell: int = 200) -> str:
        """Render as a markdown table -- the form models read most reliably."""
        if not self.columns:
            return "(no columns)"
        if not self.rows:
            return "(0 rows)"

        def cell(value: Any) -> str:
            text = "NULL" if value is None else str(value)
            text = text.replace("|", r"\|").replace("\n", " ")
            return text if len(text) <= max_cell else text[: max_cell - 1] + "\u2026"

        shown = self.rows[:max_rows]
        lines = [
            "| " + " | ".join(self.columns) + " |",
            "| " + " | ".join("---" for _ in self.columns) + " |",
        ]
        lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in shown]
        if len(self.rows) > len(shown):
            lines.append(f"\n_({len(self.rows) - len(shown)} more rows not shown)_")
        if self.truncated:
            lines.append(
                f"\n_Result was truncated at {self.row_count} rows; "
                "add an explicit LIMIT or aggregate to see the full picture._"
            )
        return "\n".join(lines)


@dataclass
class RunResult:
    """The outcome of :meth:`agent.DataAgent.run`."""

    text: str
    turns: int
    usage: Usage
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.text
