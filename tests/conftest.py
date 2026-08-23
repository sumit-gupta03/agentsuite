"""Shared fixtures.

Everything here runs against the stdlib SQLite adapter, so the suite needs no
database, no driver and no network. The agent tests drive the real loop through
a scripted fake model implementing the :class:`~agentkart.core.model.Model` protocol
-- which is the practical proof that the protocol is real and not decorative.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from agentkart.core.types import ModelTurn, ToolCall, Usage
from agentkart.domains.dataengineering.warehouse.sqlite_adapter import SQLiteWarehouse


@pytest.fixture
def warehouse() -> SQLiteWarehouse:
    """An in-memory warehouse seeded with a deliberately flawed orders table."""
    wh = SQLiteWarehouse(":memory:")
    wh.connection.executescript(
        """
        CREATE TABLE orders (
            order_id     INTEGER,
            customer_id  INTEGER,
            amount_cents INTEGER,
            status       TEXT,
            loaded_at    TEXT
        );
        INSERT INTO orders VALUES
            (1, 10,  1500, 'complete', '2026-08-01T00:00:00'),
            (2, 11,  2500, 'complete', '2026-08-01T00:00:00'),
            (3, 12,    -1, 'pending',  '2026-08-02T00:00:00'),
            (3, 12,    -1, 'pending',  '2026-08-02T00:00:00'),
            (4, 13,  9900, 'complete', NULL),
            (5, NULL, 500, 'refunded', '2026-08-03T00:00:00');

        CREATE TABLE customers (
            customer_id INTEGER,
            email       TEXT
        );
        INSERT INTO customers VALUES
            (10, 'a@example.com'),
            (11, NULL),
            (12, 'unknown@example.com');
        """
    )
    yield wh
    wh.close()


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """A directory holding one valid skill."""
    directory = tmp_path / "custom" / "house-style"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: house-style
            description: Team conventions for SQL and naming.
            ---

            # House style

            Every table carries `loaded_at`.
            """
        ),
        encoding="utf-8",
    )
    (directory / "reference").mkdir()
    (directory / "reference" / "naming.md").write_text("snake_case only.", encoding="utf-8")
    return tmp_path / "custom"


class FakeModel:
    """A scripted model that replays a fixed list of turns.

    Each script entry is either a string (a final text answer) or a list of
    ``(tool_name, input_dict)`` pairs to call.
    """

    model_id = "fake"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[dict[str, Any]]] = []
        self.seen_messages: list[list[dict[str, Any]]] = []

    def generate(  # type: ignore[no-untyped-def]
        self, *, system, messages, tools, on_text=None
    ) -> ModelTurn:
        self.seen_systems.append(system)
        self.seen_tools.append(tools)
        self.seen_messages.append(list(messages))

        if self.calls >= len(self.script):
            raise AssertionError("FakeModel ran out of script entries")
        step = self.script[self.calls]
        self.calls += 1
        usage = Usage(input_tokens=100, output_tokens=20)

        if isinstance(step, str):
            if on_text is not None:
                on_text(step)
            return ModelTurn(
                text=step,
                stop_reason="end_turn",
                usage=usage,
                assistant_message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": step}],
                },
            )

        calls = [
            ToolCall(id=f"call_{self.calls}_{i}", name=name, input=payload)
            for i, (name, payload) in enumerate(step)
        ]
        return ModelTurn(
            text="",
            tool_calls=calls,
            stop_reason="tool_use",
            usage=usage,
            assistant_message={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                    for c in calls
                ],
            },
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def tool_result_message(self, results) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    **({"is_error": True} if r.is_error else {}),
                }
                for r in results
            ],
        }


@pytest.fixture
def fake_model() -> type[FakeModel]:
    return FakeModel


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own config and skills out of the test run."""
    for key in list(os.environ):
        if key.startswith("AGENT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_USER_SKILLS", str(tmp_path / "no-user-skills"))
    monkeypatch.chdir(tmp_path)
