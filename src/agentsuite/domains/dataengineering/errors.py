"""Errors specific to the data engineering domain."""

from __future__ import annotations

from agentsuite.core.errors import ConnectionError_


class WarehouseError(ConnectionError_):
    """The warehouse adapter could not connect or execute."""


__all__ = ["WarehouseError"]
