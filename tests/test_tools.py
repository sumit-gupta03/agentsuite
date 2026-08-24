"""Warehouse and profiling tools, exercised directly against SQLite."""

from __future__ import annotations

import pytest

from agentsuite.core.errors import ToolError
from agentsuite.domains.dataengineering import WarehouseContext
from agentsuite.domains.dataengineering.policy import SqlPolicy
from agentsuite.domains.dataengineering.tools import profiling
from agentsuite.domains.dataengineering.tools import query as warehouse_tools


@pytest.fixture
def context(warehouse):  # type: ignore[no-untyped-def]
    return WarehouseContext(
        policy=SqlPolicy(max_rows=100),
        skills={},
        confirm_fn=lambda action, detail, purpose: False,
        connection=warehouse,
    )


class TestWarehouseTools:
    def test_list_tables(self, context) -> None:  # type: ignore[no-untyped-def]
        listed = warehouse_tools.list_tables(context)
        assert "- orders" in listed
        assert "- customers" in listed

    def test_describe_table(self, context) -> None:  # type: ignore[no-untyped-def]
        described = warehouse_tools.describe_table(context, "orders")
        assert "order_id" in described
        assert "amount_cents" in described

    def test_describe_missing_table_raises_tool_error(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError):
            warehouse_tools.describe_table(context, "nope")

    def test_run_query_returns_rows_and_records(self, context) -> None:  # type: ignore[no-untyped-def]
        output = warehouse_tools.run_query(
            context,
            sql="SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
            purpose="counts",
        )
        assert "complete" in output
        assert context.actions[0].purpose == "counts"
        assert context.actions[0].kind == "sql"

    def test_run_query_refuses_a_write_in_read_only(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="read-only"):
            warehouse_tools.run_query(context, sql="DELETE FROM orders", purpose="tidy")

    def test_run_query_refuses_multiple_statements(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="one at a time"):
            warehouse_tools.run_query(context, sql="SELECT 1; SELECT 2", purpose="two")

    def test_explain_query_works(self, context) -> None:  # type: ignore[no-untyped-def]
        assert "```" in warehouse_tools.explain_query(context, sql="SELECT * FROM orders")

    def test_explain_refuses_a_write(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="only read"):
            warehouse_tools.explain_query(context, sql="DROP TABLE orders")

    def test_estimate_reports_lack_of_support(self, context) -> None:  # type: ignore[no-untyped-def]
        assert "does not support" in warehouse_tools.estimate_query_cost(context, sql="SELECT 1")


class TestNarrowingAccessors:
    def test_db_without_a_warehouse_is_an_explained_tool_error(self) -> None:
        bare = WarehouseContext(policy=SqlPolicy(), skills={})
        with pytest.raises(ToolError, match="no warehouse connected"):
            _ = bare.db

    def test_dbt_dir_without_a_project_is_an_explained_tool_error(self) -> None:
        bare = WarehouseContext(policy=SqlPolicy(), skills={})
        with pytest.raises(ToolError, match="no dbt project"):
            _ = bare.dbt_dir


class TestProfiling:
    def test_profile_table_reports_nulls_and_distincts(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.profile_table(context, table="orders", columns=None)
        assert "**6 rows**" in output
        assert "order_id" in output
        assert "loaded_at" in output

    def test_profile_specific_columns(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.profile_table(context, table="orders", columns=["status"])
        assert "status" in output
        assert "amount_cents" not in output

    def test_profile_rejects_unknown_columns(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="no column"):
            profiling.profile_table(context, table="orders", columns=["nope"])

    def test_find_duplicates_finds_the_planted_one(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.find_duplicates(
            context, table="orders", key_columns=["order_id"], limit=5
        )
        assert "NOT unique" in output
        assert "| 3 | 2 |" in output

    def test_find_duplicates_reports_a_clean_key(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.find_duplicates(
            context, table="customers", key_columns=["customer_id"], limit=5
        )
        assert "is unique" in output

    def test_find_duplicates_rejects_unknown_columns(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="no column"):
            profiling.find_duplicates(context, table="orders", key_columns=["nope"], limit=5)

    def test_column_distribution(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.column_distribution(context, table="orders", column="status", top_n=10)
        assert "complete" in output
        assert "%" in output

    def test_column_distribution_rejects_unknown_column(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="no column"):
            profiling.column_distribution(context, table="orders", column="nope", top_n=5)

    def test_check_freshness_flags_null_timestamps(self, context) -> None:  # type: ignore[no-untyped-def]
        output = profiling.check_freshness(context, table="orders", timestamp_column="loaded_at")
        assert "Null timestamps present" in output
        assert "null timestamps: 1" in output

    def test_check_freshness_rejects_unknown_column(self, context) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ToolError, match="no column"):
            profiling.check_freshness(context, table="orders", timestamp_column="nope")


class TestCostCeiling:
    def test_ceiling_blocks_an_oversized_scan(self, warehouse) -> None:  # type: ignore[no-untyped-def]
        from agentsuite.domains.dataengineering.warehouse.base import CostEstimate

        context = WarehouseContext(
            policy=SqlPolicy(max_rows=100, max_scan_gb=1.0),
            skills={},
            connection=warehouse,
        )
        assert context.cost_ceiling_exceeded(CostEstimate(bytes_scanned=5_000_000_000))
        assert not context.cost_ceiling_exceeded(CostEstimate(bytes_scanned=100))
        assert not context.cost_ceiling_exceeded(CostEstimate())
