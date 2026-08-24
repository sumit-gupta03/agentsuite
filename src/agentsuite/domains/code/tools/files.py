"""File tools, all bounded by the workspace root.

Every path argument goes through :meth:`Workspace.resolve`, which refuses
anything outside the project or matching a deny pattern. There is no second
route to the filesystem: no shell, no ``open()`` on a model-supplied string.

Writing has one rule beyond the policy tiers: **the agent must read a file
before replacing it.** Overwriting something it has not looked at is how work
gets silently destroyed, so that case is classified destructive and needs
confirmation.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

from agentsuite.core.errors import ToolError
from agentsuite.core.tools import tool

from ..errors import WorkspaceError

if TYPE_CHECKING:
    from .. import WorkspaceContext


@tool(
    name="list_files",
    description=(
        "List files in the project, optionally filtered by a glob pattern. Start "
        "here to learn the layout before reading anything. Build artefacts, "
        "virtualenvs and credential files are never listed."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory relative to the project root. Empty for the root.",
            },
            "pattern": {
                "type": "string",
                "description": "Glob such as '*.py' or '**/*.tf'. Use '*' for everything.",
            },
        },
        "required": ["path", "pattern"],
    },
    requires=["workspace"],
)
def list_files(context: WorkspaceContext, path: str = "", pattern: str = "*") -> str:
    workspace = context.workspace
    try:
        found = workspace.walk(path, pattern=pattern or "*")
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc
    if not found:
        return f"(no files matching {pattern!r} under {path or 'the project root'})"

    shown = found[:400]
    lines = [f"{workspace.relative(p)}  ({p.stat().st_size:,} bytes)" for p in shown]
    if len(found) > len(shown):
        lines.append(f"... and {len(found) - len(shown):,} more; narrow the pattern")
    return "\n".join(lines)


@tool(
    name="read_file",
    description=(
        "Read a text file from the project. Always read a file before editing it -- "
        "editing something you have not read is refused. Returns the file with line "
        "numbers so you can refer to specific lines."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."},
            "start": {
                "type": "integer",
                "description": "First line to return (1-based). 0 for the whole file.",
            },
            "count": {
                "type": "integer",
                "description": "How many lines to return. 0 for all of them.",
            },
        },
        "required": ["path", "start", "count"],
    },
    requires=["workspace"],
)
def read_file(context: WorkspaceContext, path: str, start: int = 0, count: int = 0) -> str:
    workspace = context.workspace
    verdict = context.policy.check(path, kind="read")
    if not verdict.allowed:
        raise ToolError(verdict.reason)

    try:
        text = workspace.read(path)
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc

    context.mark_read(path)
    context.record(f"read {path}", "read", "inspect source", kind="file")

    lines = text.splitlines()
    begin = max(1, start or 1)
    end = len(lines) if not count else min(len(lines), begin + count - 1)
    window = lines[begin - 1 : end]
    width = len(str(end))
    body = "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(window, begin))

    header = f"{path} ({len(lines)} lines"
    header += f", showing {begin}-{end})" if (start or count) else ")"
    return f"{header}\n\n{body}"


@tool(
    name="grep",
    description=(
        "Search the project for a regular expression and return matching lines with "
        "their file and line number. Far cheaper than reading files to find something."
    ),
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "glob": {
                "type": "string",
                "description": "Limit to files matching this glob, e.g. '*.py'.",
            },
            "max_results": {"type": "integer", "description": "Cap on matches returned (1-500)."},
        },
        "required": ["pattern", "glob", "max_results"],
    },
    requires=["workspace"],
)
def grep(
    context: WorkspaceContext, pattern: str, glob: str = "*", max_results: int = 100
) -> str:
    workspace = context.workspace
    limit = max(1, min(int(max_results or 100), 500))
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc

    hits: list[str] = []
    for candidate in workspace.walk("", pattern=glob or "*"):
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{workspace.relative(candidate)}:{number}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    context.record(f"grep {pattern!r}", "read", "search source", kind="file")
    if not hits:
        return f"(no matches for {pattern!r} in {glob!r})"
    suffix = f"\n\n_capped at {limit} matches_" if len(hits) >= limit else ""
    return "\n".join(hits) + suffix


@tool(
    name="write_file",
    description=(
        "Create a file, or replace one you have already read in this session. "
        "Requires a write-enabled session. Replacing a file you have NOT read is "
        "treated as destructive and needs operator confirmation -- read it first. "
        "Returns a diff of what changed."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."},
            "content": {"type": "string", "description": "The complete new contents of the file."},
            "purpose": {
                "type": "string",
                "description": "One sentence on why. Shown in confirmation prompts and audited.",
            },
        },
        "required": ["path", "content", "purpose"],
    },
    destructive=True,
    requires=["workspace", "write"],
)
def write_file(context: WorkspaceContext, path: str, content: str, purpose: str = "") -> str:
    workspace = context.workspace
    try:
        target = workspace.resolve(path)
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc

    exists = target.exists()
    previous = workspace.read(path) if exists else ""

    verdict = context.policy.check(
        path, kind="write", exists=exists, unread=exists and not context.has_read(path)
    )
    if not verdict.allowed and not verdict.needs_confirmation:
        raise ToolError(verdict.reason)
    if verdict.needs_confirmation:
        label = verdict.primary.label if verdict.primary else "WRITE"
        approved = context.confirm(
            action=f"{label} {path}",
            detail=_diff(previous, content, path)[:4000],
            purpose=purpose,
        )
        if not approved:
            raise ToolError(
                "refused: the operator declined this write. Read the file first, or "
                "explain why the change is needed."
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    context.mark_read(path)
    context.record(f"write {path}", verdict.tier, purpose, kind="file")

    if not exists:
        return f"Created {path} ({len(content.splitlines())} lines)."
    diff = _diff(previous, content, path)
    return f"Updated {path}.\n\n```diff\n{diff}\n```" if diff else f"{path} was already identical."


@tool(
    name="edit_file",
    description=(
        "Replace an exact string in a file. Preferred over write_file for changes to "
        "existing code: it cannot accidentally discard the rest of the file. The old "
        "text must appear exactly once. Read the file first."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."},
            "old": {
                "type": "string",
                "description": "Exact text to replace. Must be unique in the file.",
            },
            "new": {"type": "string", "description": "Replacement text."},
            "purpose": {"type": "string", "description": "One sentence on why. Audited."},
        },
        "required": ["path", "old", "new", "purpose"],
    },
    requires=["workspace", "write"],
)
def edit_file(
    context: WorkspaceContext, path: str, old: str, new: str, purpose: str = ""
) -> str:
    workspace = context.workspace
    if not context.has_read(path):
        raise ToolError(
            f"read {path} before editing it. Editing text you have not seen in this "
            "session risks matching something you did not intend."
        )
    if old == new:
        raise ToolError("old and new are identical; nothing to do")

    try:
        text = workspace.read(path)
        target = workspace.resolve(path, must_exist=True)
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc

    occurrences = text.count(old)
    if occurrences == 0:
        raise ToolError(
            f"{path} does not contain that text exactly. Re-read it and match precisely."
        )
    if occurrences > 1:
        raise ToolError(
            f"that text appears {occurrences} times in {path}. Include more surrounding "
            "context so the match is unique."
        )

    verdict = context.policy.check(path, kind="write", exists=True, unread=False)
    if not verdict.allowed and not verdict.needs_confirmation:
        raise ToolError(verdict.reason)

    updated = text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    context.record(f"edit {path}", verdict.tier, purpose, kind="file")
    return f"Edited {path}.\n\n```diff\n{_diff(text, updated, path)}\n```"


def _diff(before: str, after: str, path: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    if len(lines) > 200:
        lines = lines[:200] + [f"... ({len(lines) - 200} more diff lines)"]
    return "\n".join(lines)
