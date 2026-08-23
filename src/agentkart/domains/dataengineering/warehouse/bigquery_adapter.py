"""BigQuery adapter -- ``pip install "agent[bigquery]"``.

BigQuery is the one warehouse here that will happily bill you four figures for a
typo, so :meth:`BigQueryWarehouse.estimate_cost` is a real dry-run rather than a
stub, and the tool layer calls it before every query.
"""

from __future__ import annotations

import os
import time
from typing import Any

from agentkart.core.errors import ConfigError
from agentkart.core.types import Column, QueryResult

from ..errors import WarehouseError
from .base import CostEstimate, TableRef, Warehouse

try:  # pragma: no cover - exercised only where the client is installed
    from google.cloud import bigquery
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "google-cloud-bigquery is not installed. "
        'Install it with: pip install "agent[bigquery]"'
    ) from exc

#: On-demand query pricing, USD per TiB scanned. Override per project.
DEFAULT_USD_PER_TIB = 6.25


class BigQueryWarehouse(Warehouse):
    dialect = "bigquery"
    quote_char = "`"

    def __init__(
        self,
        project: str | None = None,
        location: str | None = None,
        dataset: str | None = None,
        usd_per_tib: float = DEFAULT_USD_PER_TIB,
        **_: Any,
    ) -> None:
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not self.project:
            raise ConfigError(
                "no BigQuery project. Pass project=..., or set GOOGLE_CLOUD_PROJECT. "
                "Authentication uses Application Default Credentials."
            )
        self.location = location or os.environ.get("BIGQUERY_LOCATION")
        self.default_dataset = dataset or os.environ.get("BIGQUERY_DATASET")
        self.usd_per_tib = usd_per_tib
        try:
            self._client = bigquery.Client(project=self.project, location=self.location)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(f"cannot create a BigQuery client: {exc}") from exc

    @property
    def name(self) -> str:
        return "bigquery"

    def execute(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        started = time.perf_counter()
        try:
            job = self._client.query(sql)
            iterator = job.result(max_results=max_rows + 1)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc

        columns = [field.name for field in iterator.schema]
        fetched = [tuple(row.values()) for row in iterator]
        truncated = len(fetched) > max_rows
        rows = fetched[:max_rows]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def list_schemas(self) -> list[str]:
        return [ds.dataset_id for ds in self._client.list_datasets(project=self.project)]

    def list_tables(self, schema: str | None = None) -> list[str]:
        dataset = schema or self.default_dataset
        if not dataset:
            raise WarehouseError("no dataset given and no default dataset configured")
        self._validate_identifier(dataset, "dataset name")
        return [t.table_id for t in self._client.list_tables(f"{self.project}.{dataset}")]

    def describe_table(self, table: str) -> list[Column]:
        ref = TableRef.parse(table)
        dataset = ref.schema or self.default_dataset
        if not dataset:
            raise WarehouseError(f"{table!r} has no dataset and no default dataset is configured")
        full = f"{ref.database or self.project}.{dataset}.{ref.table}"
        try:
            meta = self._client.get_table(full)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(f"cannot describe {full!r}: {exc}") from exc
        return [
            Column(
                name=field.name,
                type=field.field_type,
                nullable=field.mode != "REQUIRED",
                comment=field.description,
            )
            for field in meta.schema
        ]

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """Dry-run the query. This is the guardrail that matters on BigQuery."""
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self._client.query(sql, job_config=config)
        except Exception as exc:  # noqa: BLE001
            return CostEstimate(note=f"dry run failed: {exc}")
        processed = int(job.total_bytes_processed or 0)
        return CostEstimate(
            bytes_scanned=processed,
            currency_estimate=processed / (1024**4) * self.usd_per_tib,
            note="dry-run estimate; on-demand pricing",
        )

    def explain(self, sql: str) -> str:
        estimate = self.estimate_cost(sql)
        return estimate.summary() if estimate else "no plan available"

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            pass


__all__ = ["BigQueryWarehouse"]
