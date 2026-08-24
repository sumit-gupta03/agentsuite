"""Errors specific to the code domain."""

from __future__ import annotations

from agentsuite.core.errors import ConnectionError_


class WorkspaceError(ConnectionError_):
    """A path escaped the project root, was denied, or could not be read."""


class CommandError(ConnectionError_):
    """An allowlisted command failed to start, or timed out."""


__all__ = ["CommandError", "WorkspaceError"]
