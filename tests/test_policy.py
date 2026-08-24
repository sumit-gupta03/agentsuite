"""The permission layer: the shared decision, and the SQL classifier.

These are the tests that matter most. A regression here means an agent can do
something the operator did not authorise.
"""

from __future__ import annotations

import pytest

from agentsuite.core.policy import Action, Policy
from agentsuite.domains.dataengineering.policy import SqlPolicy, apply_limit, referenced_tables


class _ScriptedPolicy(Policy):
    """A minimal domain policy, to test the shared decision in isolation."""

    def classify(self, request: str, **context: object) -> list[Action]:
        tier = context.get("tier", "read")
        return [Action("test", request, tier, request.upper())]  # type: ignore[arg-type]


class TestSharedDecision:
    """Behaviour every domain inherits, exercised without any SQL."""

    def test_reads_are_always_allowed(self) -> None:
        assert _ScriptedPolicy().check("look", tier="read").allowed

    def test_writes_need_a_write_session(self) -> None:
        verdict = _ScriptedPolicy().check("change", tier="write")
        assert not verdict.allowed
        assert "read-only" in verdict.reason
        assert "write=True" in verdict.reason

    def test_destructive_needs_confirmation_even_with_write(self) -> None:
        verdict = _ScriptedPolicy(write=True).check("wipe", tier="destructive")
        assert not verdict.allowed
        assert verdict.needs_confirmation

    def test_destructive_allowed_once_confirmed(self) -> None:
        policy = _ScriptedPolicy(write=True, allow_destructive=True)
        assert policy.check("wipe", tier="destructive").allowed

    def test_empty_request_is_refused(self) -> None:
        class _Empty(_ScriptedPolicy):
            def classify(self, request: str, **context: object) -> list[Action]:
                return []

        assert not _Empty().check("").allowed

    def test_describe_reports_the_mode(self) -> None:
        assert _ScriptedPolicy().describe() == "read-only"
        assert "confirmation" in _ScriptedPolicy(write=True).describe()


class TestSqlClassification:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM orders WHERE id = 1",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "SELECT a FROM t UNION SELECT b FROM u",
        ],
    )
    def test_reads(self, sql: str) -> None:
        assert SqlPolicy().classify(sql)[0].tier == "read"

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO orders VALUES (1)",
            "UPDATE orders SET status = 'x' WHERE id = 1",
            "DELETE FROM orders WHERE id = 1",
            "CREATE TABLE t (a INT)",
            "ALTER TABLE t ADD COLUMN b INT",
        ],
    )
    def test_writes(self, sql: str) -> None:
        assert SqlPolicy().classify(sql)[0].tier == "write"

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE orders",
            "TRUNCATE TABLE orders",
            "DELETE FROM orders",
            "UPDATE orders SET status = 'x'",
            "GRANT SELECT ON orders TO bob",
        ],
    )
    def test_destructive(self, sql: str) -> None:
        assert SqlPolicy().classify(sql)[0].tier == "destructive"

    def test_unfiltered_delete_is_destructive_but_filtered_is_not(self) -> None:
        assert SqlPolicy().classify("DELETE FROM t")[0].tier == "destructive"
        assert SqlPolicy().classify("DELETE FROM t WHERE id = 1")[0].tier == "write"

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("TRUNCATE TABLE orders", "TRUNCATE"),
            ("DROP TABLE orders", "DROP"),
            ("REVOKE SELECT ON orders FROM bob", "REVOKE"),
        ],
    )
    def test_destructive_statements_are_named_readably(self, sql: str, expected: str) -> None:
        """A refusal has to say what it refused, not leak a parser class name."""
        action = SqlPolicy().classify(sql)[0]
        assert action.tier == "destructive"
        assert action.label == expected
        assert "unrecognised" not in action.reason

    def test_unparseable_sql_fails_closed(self) -> None:
        """An unrecognised statement must never be treated as a read."""
        assert SqlPolicy().classify("!!! not sql at all $$$")[0].tier == "destructive"

    def test_multiple_statements_are_split(self) -> None:
        actions = SqlPolicy().classify("SELECT 1; DROP TABLE t")
        assert len(actions) == 2
        assert {a.tier for a in actions} == {"read", "destructive"}


class TestSqlPolicy:
    def test_read_only_refuses_writes(self) -> None:
        verdict = SqlPolicy().check("INSERT INTO t VALUES (1)")
        assert not verdict.allowed
        assert "read-only" in verdict.reason

    def test_write_allows_a_filtered_update(self) -> None:
        assert SqlPolicy(write=True).check("UPDATE t SET a = 1 WHERE id = 2").allowed

    def test_unfiltered_update_needs_confirmation_in_write_mode(self) -> None:
        verdict = SqlPolicy(write=True).check("UPDATE t SET a = 1")
        assert not verdict.allowed
        assert verdict.needs_confirmation

    def test_multi_statement_refused_by_default(self) -> None:
        verdict = SqlPolicy().check("SELECT 1; SELECT 2")
        assert not verdict.allowed
        assert "one at a time" in verdict.reason

    def test_multi_statement_allowed_when_enabled(self) -> None:
        assert SqlPolicy(allow_multi_statement=True).check("SELECT 1; SELECT 2").allowed

    def test_a_read_cannot_smuggle_a_drop(self) -> None:
        """The whole point: the highest tier in the batch governs."""
        policy = SqlPolicy(write=True, allow_multi_statement=True)
        verdict = policy.check("SELECT 1; DROP TABLE t")
        assert not verdict.allowed
        assert verdict.tier == "destructive"

    def test_primary_names_the_offending_action(self) -> None:
        verdict = SqlPolicy().check("DROP TABLE t")
        assert verdict.primary is not None
        assert verdict.primary.label == "DROP"

    def test_describe_mentions_the_row_cap(self) -> None:
        assert "1000 rows" in SqlPolicy().describe()


class TestAutoLimit:
    def test_bare_select_gets_a_limit(self) -> None:
        verdict = SqlPolicy(max_rows=10).check("SELECT * FROM orders")
        assert verdict.rewritten is not None
        assert "LIMIT 10" in verdict.rewritten.upper()

    def test_existing_limit_is_respected(self) -> None:
        assert SqlPolicy(max_rows=10).check("SELECT * FROM orders LIMIT 5").rewritten is None

    def test_scalar_aggregate_is_left_alone(self) -> None:
        assert apply_limit("SELECT COUNT(*) FROM orders", 10) is None

    def test_grouped_aggregate_is_limited(self) -> None:
        limited = apply_limit("SELECT status, COUNT(*) FROM orders GROUP BY status", 10)
        assert limited is not None and "LIMIT 10" in limited.upper()

    def test_can_be_disabled(self) -> None:
        assert SqlPolicy(auto_limit=False).check("SELECT * FROM orders").rewritten is None

    def test_unparseable_sql_is_left_alone(self) -> None:
        assert apply_limit("$$$ nonsense", 10) is None

    def test_writes_are_never_rewritten(self) -> None:
        verdict = SqlPolicy(write=True).check("INSERT INTO t VALUES (1)")
        assert verdict.allowed
        assert verdict.rewritten is None


class TestScanCeiling:
    def test_ceiling_blocks_an_oversized_scan(self) -> None:
        policy = SqlPolicy(max_scan_gb=1.0)
        assert policy.exceeds_scan_ceiling(5.0)
        assert not policy.exceeds_scan_ceiling(0.1)
        assert not policy.exceeds_scan_ceiling(None)

    def test_no_ceiling_means_no_limit(self) -> None:
        assert not SqlPolicy().exceeds_scan_ceiling(10_000.0)


def test_referenced_tables() -> None:
    tables = referenced_tables("SELECT * FROM raw.orders o JOIN raw.customers c ON o.id = c.id")
    assert any("orders" in t for t in tables)
    assert any("customers" in t for t in tables)
