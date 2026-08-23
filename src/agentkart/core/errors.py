"""Exception hierarchy.

Everything raised by this package derives from :class:`AgentError`, so a caller
can wrap a run in a single ``except`` without swallowing unrelated failures.

Domain-specific failures subclass these in the domain package -- nothing here
knows what a warehouse or a build system is.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(AgentError):
    """Configuration could not be resolved or is internally inconsistent."""


class SkillError(AgentError):
    """A skill file could not be parsed, or a requested skill does not exist."""


class ToolError(AgentError):
    """A tool failed in a way the agent should see as a tool result, not a crash."""


class GuardrailError(ToolError):
    """An action was refused by the permission layer.

    Deliberately a :class:`ToolError`: a blocked action is fed back to the model
    as an error result so it can choose a safer approach, rather than tearing
    down the run.
    """


class ConfirmationDenied(ToolError):
    """A destructive action was declined by the confirmation callback."""


class ConnectionError_(AgentError):
    """A domain's connection could not be established or used.

    Domains subclass this -- ``WarehouseError`` for the data domain, and so on --
    so callers can catch either the specific one or all of them.
    """


class ModelError(AgentError):
    """The language model backend failed or returned something unusable."""


class RefusalError(ModelError):
    """The model declined the request outright."""

    def __init__(self, message: str, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


class MaxTurnsExceeded(AgentError):
    """The agent loop hit its turn budget without producing a final answer."""


__all__ = [
    "AgentError",
    "ConfigError",
    "ConfirmationDenied",
    "ConnectionError_",
    "GuardrailError",
    "MaxTurnsExceeded",
    "ModelError",
    "RefusalError",
    "SkillError",
    "ToolError",
]
