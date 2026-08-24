"""Reconciliation tools, against real (small) tables.

The planted discrepancies are the ones that occur in practice: a duplicated key,
a dropped row, and a value that changed without the row count moving.
"""

from __future__ import annotations

import pytest

from agentsuite.core.errors import ToolError
from agentsuite.domains.dataengineering import WarehouseContext
from agentsuite.domains.dataengineering.policy import SqlPolicy
from agentsuite.domains.dataengineering.tools import reconcile
from agentsuite.domains.dataengineering.warehouse.sqlite_adapter import SQLiteWarehouse


@pytest.fixture
def context() -> WarehouseContext:
    warehouse = SQLiteWarehouse(":memory:")
    warehouse.connection.executescript(
        """
        CREATE TABLE source (order_id INTEGER, amount_cents INTEGER, loaded_at TEXT);
        INSERT INTO source VALUES
            (1, 100, '2026-08-01'), (2, 200, '2026-08-01'),
            (3, 300, '2026-08-02'), (4, 400, '2026-08-02'),
            (5, 500, '2026-08-03');

        -- target: key 3 duplicated, key 5 missing, key 4's amount changed
        CREATE TABLE target (order_id INTEGER, amount_cents INTEGER, loaded_at TEXT);
        INSERT INTO target VALUES
            (1, 100, '2026-08-01'), (2, 200, '2026-08-01'),
            (3, 300, '2026-08-02'), (3, 300, '2026-08-02'),
            (4, 999, '2026-08-02');

        -- a clean copy, for the agreeing case
        CREATE TABLE clean_copy (order_id INTEGER, amount_cents INTEGER, loaded_at TEXT);
        INSERT INTO clean_copy SELECT * FROM source;
        """
    )
    ctx = WarehouseContext(
        policy=SqlPolicy(max_rows=100), skills={}, connection=warehouse
    )
    yield ctx
    warehouse.close()


class TestCompareTables:
    def test_reports_a_matching_pair_as_agreeing(self, context: WarehouseContext) -> None:
        output = reconcile.compare_tables(
            context, source="source", target="clean_copy",
            key_columns=["order_id"], sum_column="amount_cents",
        )
        assert "agree on every check" in output

    def test_detects_a_duplicated_key(self, context: WarehouseContext) -> None:
        output = reconcile.compare_tables(
            context, source="source", target="target", key_columns=["order_id"], sum_column=None,
        )
        assert "not unique on the key" in output
        assert "1 duplicate row" in output

    def test_detects_a_missing_key(self, context: WarehouseContext) -> None:
        output = reconcile.compare_tables(
            context, source="source", target="target", key_columns=["order_id"], sum_column=None,
        )
        assert "1 key(s) are missing from the target" in output

    def test_detects_a_changed_value_via_the_checksum(self, context: WarehouseContext) -> None:
        """Counts can agree while values do not -- this is the check that catches it."""
        output = reconcile.compare_tables(
            context, source="source", target="target",
            key_columns=["order_id"], sum_column="amount_cents",
        )
        assert "SUM(amount_cents)` differs" in output

    def test_samples_the_missing_keys(self, context: WarehouseContext) -> None:
        output = reconcile.compare_tables(
            context, source="source", target="target", key_columns=["order_id"], sum_column=None,
        )
        assert "Sample keys missing" in output
        assert "`5`" in output

    def test_rejects_an_unknown_column(self, context: WarehouseContext) -> None:
        with pytest.raises(ToolError, match="no column"):
            reconcile.compare_tables(
                context, source="source", target="target",
                key_columns=["nope"], sum_column=None,
            )

    def test_rejects_an_empty_key(self, context: WarehouseContext) -> None:
        with pytest.raises(ToolError, match="at least one column"):
            reconcile.compare_tables(
                context, source="source", target="target", key_columns=[], sum_column=None,
            )

    def test_records_the_action(self, context: WarehouseContext) -> None:
        reconcile.compare_tables(
            context, source="source", target="clean_copy",
            key_columns=["order_id"], sum_column=None,
        )
        assert context.actions[-1].purpose == "reconciliation"


class TestCompareByPeriod:
    def test_localises_the_difference_to_a_day(self, context: WarehouseContext) -> None:
        output = reconcile.compare_by_period(
            context, source="source", target="target", date_column="loaded_at", limit=30,
        )
        assert "2026-08-03" in output, "the day with the missing row"
        assert "2026-08-01" not in output, "days that agree are not listed"

    def test_agreeing_periods_say_so_with_a_caveat(self, context: WarehouseContext) -> None:
        output = reconcile.compare_by_period(
            context, source="source", target="clean_copy", date_column="loaded_at", limit=30,
        )
        assert "agree for every period" in output
        assert "does not mean the values agree" in output

    def test_rejects_an_unknown_date_column(self, context: WarehouseContext) -> None:
        with pytest.raises(ToolError, match="no column"):
            reconcile.compare_by_period(
                context, source="source", target="target", date_column="nope", limit=10,
            )
