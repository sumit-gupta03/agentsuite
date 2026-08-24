"""The data engineering domain.

Tools for warehouses: query, profile, explain, dbt. Presets pin the domain to one
warehouse and layer on its skills -- ``agent.snowflake()`` is this domain with
``warehouse="snowflake"``, not a separate agent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentsuite.core.config import Config
from agentsuite.core.domain import Domain, resolve_preset
from agentsuite.core.loop import Agent, AgentContext
from agentsuite.core.policy import Policy

from .policy import SqlPolicy
from .warehouse import connect
from .warehouse.base import CostEstimate, Warehouse

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
## Working on a warehouse

- Call `describe_table` before writing SQL against a table. Inferred column names
  are the largest single source of failed queries.
- Claims about data quality need `profile_table`, `find_duplicates` or
  `check_freshness` behind them, not inference from column names.
- On a large table, `explain_query` or `estimate_query_cost` before you scan.
- One statement per `run_query` call, so each can be reviewed and reverted
  independently.\
"""


class WarehouseContext(AgentContext):
    """Agent context with the warehouse narrowed to non-optional."""

    @property
    def db(self) -> Warehouse:
        """The warehouse, or a tool error explaining that there isn't one.

        Warehouse tools are only registered when a warehouse is connected, so
        this should never fire in normal use -- it exists so a hand-registered
        tool fails with an explanation rather than an AttributeError.
        """
        from agentsuite.core.errors import ToolError

        if not isinstance(self.connection, Warehouse):
            raise ToolError(
                "this session has no warehouse connected. Pass warehouse=... to use "
                "warehouse tools."
            )
        return self.connection

    @property
    def dbt_dir(self) -> Path:
        """The dbt project directory, or a tool error explaining that there isn't one."""
        from agentsuite.core.errors import ToolError

        found = dbt_project_dir(self.config) if self.config else None
        if found is None:
            raise ToolError(
                "this session has no dbt project. Pass dbt_project_dir=... pointing at "
                "the directory that contains dbt_project.yml."
            )
        return found

    @property
    def sql_policy(self) -> SqlPolicy:
        assert isinstance(self.policy, SqlPolicy)
        return self.policy

    def cost_ceiling_exceeded(self, estimate: CostEstimate) -> bool:
        gigabytes = estimate.bytes_scanned / 1e9 if estimate.bytes_scanned is not None else None
        return self.sql_policy.exceeds_scan_ceiling(gigabytes)


def _build_policy(config: Config) -> Policy:
    """Every limit here is a config option, not a constant baked into the class."""
    return SqlPolicy(
        write=config.write,
        allow_destructive=False,
        allow_multi_statement=bool(config.option("allow_multi_statement", False)),
        max_rows=int(config.option("max_rows", 1000)),
        auto_limit=bool(config.option("auto_limit", True)),
        max_scan_gb=_as_float(config.option("max_scan_gb")),
    )


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _build_connection(config: Config) -> Warehouse | None:
    target = config.option("warehouse")
    return connect(target) if target else None


def dbt_project_dir(config: Config) -> Path | None:
    """The configured dbt project, but only if it really is one.

    A path without ``dbt_project.yml`` is not a dbt project, and loading dbt
    tools against it would give the model six tools that fail on every call.
    """
    raw = config.option("dbt_project_dir")
    if not raw:
        return None
    path = Path(str(raw)).expanduser().resolve()
    if not (path / "dbt_project.yml").is_file():
        logger.warning(
            "%s has no dbt_project.yml, so dbt tools and skills are unavailable. "
            "Point dbt_project_dir at the directory containing dbt_project.yml.",
            path,
        )
        return None
    return path


def _capabilities(config: Config, connection: Any) -> set[str]:
    caps: set[str] = set()
    if isinstance(connection, Warehouse):
        caps.add("warehouse")
        if connection.dialect:
            caps.add(connection.dialect)
    if dbt_project_dir(config) is not None:
        caps.add("dbt")
    return caps


DOMAIN = Domain(
    name="dataengineering",
    description="warehouses, pipelines, SQL and dbt",
    package=__name__,
    tool_modules=("tools.query", "tools.profiling", "tools.reconcile", "tools.dbt"),
    policy_factory=_build_policy,
    connection_factory=_build_connection,
    capability_factory=_capabilities,
    extra="dataengineering",
    aliases=("de", "data"),
    presets={
        # Presets are configuration, not code. A "Snowflake Agent" is this
        # domain with one option pinned -- adding another costs a dict entry.
        "snowflake": {"warehouse": "snowflake"},
        "bigquery": {"warehouse": "bigquery"},
        "duckdb": {"warehouse": "duckdb"},
        "postgres": {"warehouse": "postgres"},
        "sql": {"disable_skills": ["dbt-model-authoring", "incremental-backfill"]},
        # dbt pins nothing -- it exists so agent.dbt(...) reads naturally, and
        # so create() can insist on a dbt_project_dir rather than silently
        # handing back an agent with no dbt tools.
        "dbt": {},
        "reconciliation": {"disable_skills": ["dbt-model-authoring"]},
        "reconcile": {"disable_skills": ["dbt-model-authoring"]},
    },
    preset_descriptions={
        "snowflake": "Query, profile or debug data in a Snowflake warehouse.",
        "bigquery": "Query, profile or debug data in BigQuery, including scan cost.",
        "duckdb": "Query or profile data in DuckDB.",
        "postgres": "Query or profile data in PostgreSQL.",
        "sql": (
            "Write, review or debug SQL against a warehouse: joins, aggregates, "
            "window functions, query plans and performance."
        ),
        "dbt": (
            "Work on a dbt project: models, materialisations, incremental logic, "
            "lineage, dbt tests, compiling and running models."
        ),
        "reconciliation": (
            "Check whether two tables or datasets agree after a migration, backfill "
            "or rebuild; find where and since when they diverged; investigate numbers "
            "that do not tie out."
        ),
    },
)


def create(
    warehouse: str | dict[str, Any] | Warehouse | None = None,
    *,
    preset: str | None = None,
    dbt_project_dir: str | Path | None = None,
    max_rows: int | None = None,
    max_scan_gb: float | None = None,
    allow_multi_statement: bool | None = None,
    **kwargs: Any,
) -> Agent:
    """Build a data engineering agent.

    ::

        import agentsuite as agent
        de = agent.dataengineering(warehouse="duckdb")
        de.run("Profile raw.orders and flag anything that breaks a join")
    """
    options = resolve_preset(
        DOMAIN,
        preset,
        {
            "warehouse": warehouse if not isinstance(warehouse, Warehouse) else None,
            "dbt_project_dir": str(dbt_project_dir) if dbt_project_dir else None,
            "max_rows": max_rows,
            "max_scan_gb": max_scan_gb,
            "allow_multi_statement": allow_multi_statement,
        },
    )
    options = {k: v for k, v in options.items() if v is not None}

    if preset == "dbt" and not options.get("dbt_project_dir"):
        from agentsuite.core.errors import ConfigError

        raise ConfigError(
            "agent.dbt(...) needs dbt_project_dir pointing at the directory that "
            "contains dbt_project.yml, e.g. agent.dbt(warehouse='snowflake', "
            "dbt_project_dir='./analytics')."
        )

    disable = options.pop("disable_skills", None)
    if disable:
        kwargs.setdefault("disable_skills", disable)

    return Agent(
        domain=DOMAIN,
        connection=warehouse if isinstance(warehouse, Warehouse) else None,
        context_class=WarehouseContext,
        **options,
        **kwargs,
    )


__all__ = ["DOMAIN", "SYSTEM_PROMPT", "WarehouseContext", "create"]
