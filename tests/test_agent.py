"""The agent loop, tool dispatch and the confirmation gate.

Every test here drives the real loop -- the same code path a live model takes.
Only the model is scripted, which is what makes these fast and deterministic
while still exercising skills, policy and dispatch end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentkart as lib
from agentkart.core.errors import MaxTurnsExceeded
from agentkart.core.loop import deny_all


def build(model_cls, script, **kwargs):  # type: ignore[no-untyped-def]
    """A data engineering agent driven by a scripted model."""
    return lib.dataengineering(model=model_cls(script), **kwargs)


class TestLoop:
    def test_returns_a_direct_answer(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["The answer is 42."])
        result = de.run("How many?")
        assert result.text == "The answer is 42."
        assert result.turns == 1
        assert result.usage.output_tokens == 20

    def test_executes_a_tool_then_answers(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("list_tables", {"schema": None})], "There are two tables."],
            warehouse=warehouse,
        )
        result = de.run("What tables exist?")
        assert result.turns == 2
        assert [c.name for c in result.tool_calls] == ["list_tables"]
        assert "orders" in de.messages[2]["content"][0]["content"]

    def test_parallel_tool_calls_return_in_one_message(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [
                [
                    ("describe_table", {"table": "orders"}),
                    ("describe_table", {"table": "customers"}),
                ],
                "Both described.",
            ],
            warehouse=warehouse,
        )
        de.run("Describe both tables")
        assert len(de.messages[2]["content"]) == 2, "results must share one user message"

    def test_conversation_persists_across_runs(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["First.", "Second."])
        de.run("one")
        de.run("two")
        assert len(de.messages) == 4

    def test_reset_clears_the_transcript(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["First.", "Second."])
        de.run("one")
        de.reset()
        de.run("two")
        assert len(de.messages) == 2

    def test_max_turns_is_enforced(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        script = [[("list_tables", {"schema": None})]] * 5
        de = build(fake_model, script, warehouse=warehouse, max_turns=3)
        with pytest.raises(MaxTurnsExceeded, match="3 turns"):
            de.run("loop forever")

    def test_usage_accumulates_across_runs(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["a", "b"])
        de.run("one")
        de.run("two")
        assert de.usage.output_tokens == 40


class TestDispatch:
    def test_unknown_tool_becomes_an_error_result(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, [[("no_such_tool", {})], "Recovered."])
        result = de.run("go")
        assert result.text == "Recovered."
        tool_result = de.messages[2]["content"][0]
        assert tool_result["is_error"] is True
        assert "no such tool" in tool_result["content"]

    def test_bad_arguments_become_an_error_result(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("describe_table", {"wrong_arg": 1})], "Recovered."],
            warehouse=warehouse,
        )
        de.run("go")
        assert de.messages[2]["content"][0]["is_error"] is True

    def test_a_failing_tool_does_not_end_the_run(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("describe_table", {"table": "does_not_exist"})], "Table is missing."],
            warehouse=warehouse,
        )
        assert de.run("describe it").text == "Table is missing."
        assert de.messages[2]["content"][0]["is_error"] is True


class TestPolicyEnforcement:
    def test_read_only_agent_refuses_a_write(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("run_query", {"sql": "DROP TABLE orders", "purpose": "cleanup"})], "Refused."],
            warehouse=warehouse,
        )
        de.run("drop it")
        result = de.messages[2]["content"][0]
        assert result["is_error"] is True
        assert "read-only" in result["content"]
        assert "orders" in warehouse.list_tables()  # and the table is still there

    def test_destructive_needs_confirmation_even_with_write(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("run_query", {"sql": "DROP TABLE orders", "purpose": "cleanup"})], "Declined."],
            warehouse=warehouse,
            write=True,
        )
        de.run("drop it")
        assert "declined" in de.messages[2]["content"][0]["content"].lower()
        assert "orders" in warehouse.list_tables()

    def test_confirmation_callback_can_allow_it(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        seen: dict[str, str] = {}

        def approve(action: str, detail: str, purpose: str) -> bool:
            seen.update(action=action, detail=detail, purpose=purpose)
            return True

        de = build(
            fake_model,
            [[("run_query", {"sql": "DROP TABLE orders", "purpose": "cleanup"})], "Dropped."],
            warehouse=warehouse,
            write=True,
            confirm=approve,
        )
        de.run("drop it")
        assert seen["purpose"] == "cleanup"
        assert "DROP" in seen["detail"]
        assert "orders" not in warehouse.list_tables()

    def test_default_confirmation_denies(self) -> None:
        assert deny_all("DROP", "DROP TABLE t", "because") is False

    def test_auto_limit_is_applied_and_recorded(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("run_query", {"sql": "SELECT * FROM orders", "purpose": "peek"})], "Done."],
            warehouse=warehouse,
            max_rows=2,
        )
        de.run("show orders")
        assert "automatic LIMIT 2" in de.messages[2]["content"][0]["content"]
        assert de.actions[0].tier == "read"
        assert de.actions[0].purpose == "peek"


class TestSkillIntegration:
    def test_skill_index_is_in_the_system_prompt_but_not_the_body(
        self, fake_model, skill_dir: Path
    ) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"], skills=skill_dir)
        assert "house-style" in de.system_prompt
        assert "Team conventions" in de.system_prompt
        # The body must NOT be in the prompt -- that is the whole point.
        assert "Every table carries" not in de.system_prompt

    def test_load_skill_returns_the_body_and_records_use(
        self, fake_model, skill_dir: Path
    ) -> None:  # type: ignore[no-untyped-def]
        de = build(
            fake_model,
            [[("load_skill", {"name": "house-style"})], "Loaded."],
            skills=skill_dir,
        )
        de.run("what are our conventions?")
        assert "Every table carries" in de.messages[2]["content"][0]["content"]
        assert de.skills_used == ["house-style"]

    def test_loading_an_unknown_skill_is_an_error_result(
        self, fake_model, skill_dir: Path
    ) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, [[("load_skill", {"name": "nope"})], "Not found."], skills=skill_dir)
        de.run("go")
        assert de.messages[2]["content"][0]["is_error"] is True

    def test_dbt_tools_and_skills_absent_without_a_dbt_project(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"])
        assert not any(name.startswith("dbt_") for name in de.tools.names())
        assert "dbt-model-authoring" not in de.skills

    def test_warehouse_tools_absent_without_a_warehouse(self, fake_model) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"])
        assert "run_query" not in de.tools
        assert "load_skill" in de.tools, "skill tools are core and always present"


class TestIntrospection:
    def test_describe_shows_provenance(self, fake_model, skill_dir: Path, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"], skills=skill_dir, warehouse=warehouse)
        described = de.describe()
        assert "dataengineering" in described
        assert "house-style" in described
        assert "[explicit]" in described
        assert "read-only" in described

    def test_tool_definitions_are_strict_and_stable(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"], warehouse=warehouse)
        definitions = de.tools.to_anthropic()
        assert all(d["strict"] is True for d in definitions)
        assert all(d["input_schema"]["additionalProperties"] is False for d in definitions)
        # Byte-stable ordering keeps the prompt cache warm.
        assert definitions == de.tools.to_anthropic()

    def test_repr_is_informative(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"], warehouse=warehouse)
        text = repr(de)
        assert "dataengineering" in text
        assert "sqlite" in text
        assert " ro " in text

    def test_domain_prompt_is_included(self, fake_model, warehouse) -> None:  # type: ignore[no-untyped-def]
        de = build(fake_model, ["ok"], warehouse=warehouse)
        assert "Working on a warehouse" in de.system_prompt
        assert "describe_table" in de.system_prompt
