"""Quality gates: lint and type-check.

Cheap, fast, read-only, and the difference between code that looks right and code
that is right. The domain prompt requires these after any change, which is a
large part of what "world class" means in practice -- not cleverness, but the
discipline of verifying.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from agentkart.core.tools import tool

from .execute import run_argv

if TYPE_CHECKING:
    from .. import WorkspaceContext


@tool(
    name="lint",
    description=(
        "Run ruff over the project and report style and correctness findings. Run "
        "this after writing or editing any Python. Fast enough to run every time."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory to lint. Empty for the whole project.",
            }
        },
        "required": ["path"],
    },
    requires=["workspace"],
)
def lint(context: WorkspaceContext, path: str = "") -> str:
    if shutil.which("ruff") is None:
        return (
            "ruff is not installed in this environment, so linting was skipped. "
            "Do not claim the code is lint-clean."
        )
    argv = ["ruff", "check", "--output-format=concise"]
    argv.append(context.safe_relative(path) if path else ".")
    return run_argv(context, argv, purpose="lint")


@tool(
    name="typecheck",
    description=(
        "Run mypy over the project and report type errors. Use this after changing "
        "signatures or adding new modules; it catches a class of bug tests often miss."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory to check. Empty for the whole project.",
            }
        },
        "required": ["path"],
    },
    requires=["workspace"],
)
def typecheck(context: WorkspaceContext, path: str = "") -> str:
    if shutil.which("mypy") is None:
        return (
            "mypy is not installed in this environment, so type checking was skipped. "
            "Do not claim the code type-checks."
        )
    argv = ["mypy", context.safe_relative(path) if path else "."]
    return run_argv(context, argv, purpose="type check")


@tool(
    name="format_check",
    description=(
        "Check formatting with ruff without changing anything. Report differences; "
        "do not reformat files the task did not ask you to touch."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory. Empty for the project."}
        },
        "required": ["path"],
    },
    requires=["workspace"],
)
def format_check(context: WorkspaceContext, path: str = "") -> str:
    if shutil.which("ruff") is None:
        return "ruff is not installed, so formatting was not checked."
    argv = ["ruff", "format", "--diff", context.safe_relative(path) if path else "."]
    return run_argv(context, argv, purpose="check formatting")
