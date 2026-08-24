"""The code domain's classifier.

:class:`~agentsuite.core.policy.Policy` owns the decision -- tiers, the write gate,
the confirmation gate, fail-closed. This module answers only the question it
cannot answer generically: *what kind of action is this?*

Two kinds of action exist here, and they fail in different ways:

**Paths.** Reading is free. Writing inside the workspace is a write. Anything
that resolves outside it, or matches a deny pattern, is destructive and will not
be reached by any tool -- :class:`~agentsuite.domains.code.workspace.Workspace` has
already refused it.

**Commands.** Only an allowlisted executable may run, and only as an argument
list. A command that is not on the list is destructive, not "probably fine".
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from agentsuite.core.policy import Action, Policy

#: Executables the agent may run, and the tier each one sits at.
#:
#: Everything here either reads the project or runs its tests. Nothing on this
#: list installs packages, touches the network by design, or mutates state --
#: those belong to the operator, not the agent.
DEFAULT_COMMANDS: dict[str, str] = {
    "python": "read",
    "python3": "read",
    "pytest": "read",
    "ruff": "read",
    "mypy": "read",
    "pyright": "read",
    "black": "read",
    "isort": "read",
    "flake8": "read",
    "pylint": "read",
    "terraform": "read",  # subcommand is checked separately; apply is refused
    "tflint": "read",
    "spark-submit": "read",
}

#: Terraform subcommands, by tier. Anything absent is destructive.
TERRAFORM_SUBCOMMANDS: dict[str, str] = {
    "validate": "read",
    "fmt": "read",
    "plan": "read",
    "version": "read",
    "providers": "read",
    "graph": "read",
    "output": "read",
    "show": "read",
    "init": "write",
    "apply": "destructive",
    "destroy": "destructive",
    "import": "destructive",
    "taint": "destructive",
    "untaint": "destructive",
    "state": "destructive",
    "force-unlock": "destructive",
    "workspace": "destructive",
}

#: Argument fragments that turn an otherwise-read command destructive.
DANGEROUS_ARGS = (
    "-auto-approve",
    "--auto-approve",
    "--force",
    "-rf",
    "--no-verify",
)


@dataclass(frozen=True)
class WorkspacePolicy(Policy):
    """Permission policy for reading, writing and running code in a project."""

    #: Allowlisted executables. Anything else is refused.
    commands: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COMMANDS))
    #: Seconds any single command may run before being killed.
    timeout: int = 900

    def describe(self) -> str:
        base = "read-only" if not self.write else "may create and edit files in the project"
        return f"{base}; only allowlisted commands run, and never through a shell"

    def classify(self, request: str, **context: Any) -> list[Action]:
        kind = str(context.get("kind", "")).lower()
        if kind in {"read", "file_read"}:
            return [Action("file", request, "read", "READ")]
        if kind in {"write", "file_write"}:
            return [self._classify_write(request, context)]
        if kind in {"delete", "file_delete"}:
            return [Action("file", request, "destructive", "DELETE", "removes a file")]
        if kind in {"command", "run"}:
            return [self._classify_command(request, context)]
        # An unrecognised action kind is a programming error, and fails closed.
        return [
            Action(
                "unknown", request, "destructive", "UNKNOWN",
                f"unrecognised action kind {kind!r}",
            )
        ]

    # -- paths --------------------------------------------------------------

    @staticmethod
    def _classify_write(request: str, context: dict[str, Any]) -> Action:
        if context.get("outside_workspace"):
            return Action(
                "file", request, "destructive", "WRITE", "path resolves outside the project"
            )
        if context.get("exists") and context.get("unread"):
            # Overwriting something the agent has not looked at is how work gets
            # silently destroyed. Make it a decision, not a side effect.
            return Action(
                "file",
                request,
                "destructive",
                "OVERWRITE",
                "would replace an existing file the agent has not read",
            )
        return Action("file", request, "write", "WRITE")

    # -- commands -----------------------------------------------------------

    def _classify_command(self, request: str, context: dict[str, Any]) -> Action:
        argv = context.get("argv")
        if not argv:
            try:
                argv = shlex.split(request)
            except ValueError:
                argv = []
        if not argv:
            return Action("command", request, "destructive", "COMMAND", "empty command")

        rendered = " ".join(str(a) for a in argv)
        executable = str(argv[0]).replace("\\", "/").rsplit("/", 1)[-1]
        executable = executable[:-4] if executable.endswith(".exe") else executable

        tier = self.commands.get(executable)
        if tier is None:
            allowed = ", ".join(sorted(self.commands))
            return Action(
                "command",
                rendered,
                "destructive",
                executable.upper() or "COMMAND",
                f"{executable!r} is not an allowed executable. Allowed: {allowed}",
            )

        for argument in argv[1:]:
            if str(argument) in DANGEROUS_ARGS:
                return Action(
                    "command",
                    rendered,
                    "destructive",
                    executable.upper(),
                    f"{argument} bypasses a safety check",
                )

        if executable == "terraform":
            return self._classify_terraform(rendered, [str(a) for a in argv[1:]])

        return Action("command", rendered, tier, executable.upper())  # type: ignore[arg-type]

    @staticmethod
    def _classify_terraform(rendered: str, arguments: list[str]) -> Action:
        subcommand = next((a for a in arguments if not a.startswith("-")), "")
        tier = TERRAFORM_SUBCOMMANDS.get(subcommand)
        if tier is None:
            return Action(
                "command",
                rendered,
                "destructive",
                f"TERRAFORM {subcommand or '?'}".strip(),
                f"unrecognised terraform subcommand {subcommand!r}",
            )
        reason = ""
        if tier == "destructive":
            reason = f"terraform {subcommand} changes real infrastructure"
        return Action(
            "command", rendered, tier, f"TERRAFORM {subcommand.upper()}", reason  # type: ignore[arg-type]
        )


__all__ = ["DANGEROUS_ARGS", "DEFAULT_COMMANDS", "TERRAFORM_SUBCOMMANDS", "WorkspacePolicy"]
