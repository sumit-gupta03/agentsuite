"""Warehouse adapters.

Every adapter implements :class:`Warehouse`. Adapters are constructed through
:func:`connect`, which resolves a URL-ish string or a config mapping to a
concrete class and imports its driver lazily -- installing this package must not
drag in five database drivers.
"""

from __future__ import annotations

import importlib
from typing import Any

from agentkart.core.errors import ConfigError

from ..errors import WarehouseError
from .base import CostEstimate, TableRef, Warehouse

#: adapter name -> "module:classname" inside this package
REGISTRY: dict[str, str] = {
    "sqlite": "agentkart.domains.dataengineering.warehouse.sqlite_adapter:SQLiteWarehouse",
    "duckdb": "agentkart.domains.dataengineering.warehouse.duckdb_adapter:DuckDBWarehouse",
    "postgres": "agentkart.domains.dataengineering.warehouse.postgres_adapter:PostgresWarehouse",
    "postgresql": "agentkart.domains.dataengineering.warehouse.postgres_adapter:PostgresWarehouse",
    "snowflake": "agentkart.domains.dataengineering.warehouse.snowflake_adapter:SnowflakeWarehouse",
    "bigquery": "agentkart.domains.dataengineering.warehouse.bigquery_adapter:BigQueryWarehouse",
}

#: adapter name -> pip extra that provides its driver
EXTRAS: dict[str, str] = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "postgresql": "postgres",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
}


def register(name: str, target: str) -> None:
    """Register a third-party adapter as ``"package.module:ClassName"``."""
    REGISTRY[name.lower()] = target


def load_adapter(name: str) -> Any:
    """Import and return the adapter class for ``name``.

    Returns ``Any`` rather than ``type[Warehouse]``: each adapter takes its own
    constructor keywords, which the abstract base cannot describe.
    """
    key = name.lower()
    if key not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise ConfigError(f"unknown warehouse {name!r}. Known adapters: {known}")
    module_path, _, class_name = REGISTRY[key].partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        extra = EXTRAS.get(key)
        hint = f' Install it with: pip install "agent[{extra}]"' if extra else ""
        raise WarehouseError(f"adapter {name!r} could not be imported: {exc}.{hint}") from exc
    return getattr(module, class_name)


def connect(target: str | dict[str, Any] | Warehouse, **kwargs: Any) -> Warehouse:
    """Build a warehouse connection.

    Accepts an already-constructed :class:`Warehouse` (returned unchanged), a
    mapping with a ``type`` key, or a string -- either a bare adapter name
    (``"duckdb"``) or a DSN whose scheme names the adapter
    (``"postgresql://user@host/db"``).
    """
    if isinstance(target, Warehouse):
        return target

    if isinstance(target, dict):
        config = dict(target)
        name = config.pop("type", None)
        if not name:
            raise ConfigError("warehouse config must include a 'type' key")
        config.update(kwargs)
        return load_adapter(str(name))(**config)

    if "://" in target:
        scheme = target.split("://", 1)[0]
        return load_adapter(scheme)(dsn=target, **kwargs)

    return load_adapter(target)(**kwargs)


__all__ = [
    "EXTRAS",
    "REGISTRY",
    "CostEstimate",
    "TableRef",
    "Warehouse",
    "connect",
    "load_adapter",
    "register",
]
