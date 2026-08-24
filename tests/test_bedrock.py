"""The Bedrock backend's encoding, against a stub client.

The wire is not exercised here -- that needs live AWS credentials. What is
covered is every translation between this library's shapes and the Converse
API's, which is where the bugs would be.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentsuite.core.bedrock import BedrockModel, _to_turn, _tool_spec
from agentsuite.core.errors import ModelError
from agentsuite.core.types import ToolResult


class StubClient:
    """Records the request and returns a scripted Converse response."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hello."}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 12, "outputTokens": 4},
        }
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.response


@pytest.fixture
def model() -> BedrockModel:
    return BedrockModel("amazon.nova-pro-v1:0", client=StubClient())


class TestRequestEncoding:
    def test_sends_the_model_id_and_system_prompt(self, model: BedrockModel) -> None:
        model.generate(system="You are a data agent.", messages=[], tools=[])
        request = model._client.requests[0]
        assert request["modelId"] == "amazon.nova-pro-v1:0"
        assert request["system"] == [{"text": "You are a data agent."}]

    def test_translates_tool_definitions_to_toolspec(self, model: BedrockModel) -> None:
        """Tool authors write one definition; each backend translates it."""
        anthropic_tool = {
            "name": "run_query",
            "description": "Run SQL.",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}},
        }
        model.generate(system="s", messages=[], tools=[anthropic_tool])
        spec = model._client.requests[0]["toolConfig"]["tools"][0]["toolSpec"]
        assert spec["name"] == "run_query"
        assert spec["inputSchema"]["json"]["properties"]["sql"]["type"] == "string"

    def test_omits_toolconfig_when_there_are_no_tools(self, model: BedrockModel) -> None:
        model.generate(system="s", messages=[], tools=[])
        assert "toolConfig" not in model._client.requests[0]

    def test_temperature_is_only_sent_when_set(self) -> None:
        without = BedrockModel("m", client=StubClient())
        without.generate(system="s", messages=[], tools=[])
        assert "temperature" not in without._client.requests[0]["inferenceConfig"]

        with_temp = BedrockModel("m", temperature=0.2, client=StubClient())
        with_temp.generate(system="s", messages=[], tools=[])
        assert with_temp._client.requests[0]["inferenceConfig"]["temperature"] == 0.2

    def test_user_message_shape(self, model: BedrockModel) -> None:
        assert model.user_message("hi") == {"role": "user", "content": [{"text": "hi"}]}

    def test_tool_results_share_one_message(self, model: BedrockModel) -> None:
        """Splitting results teaches the model to stop calling tools in parallel."""
        encoded = model.tool_result_message(
            [ToolResult("a", "first"), ToolResult("b", "second")]
        )
        assert encoded["role"] == "user"
        assert len(encoded["content"]) == 2
        assert encoded["content"][0]["toolResult"]["toolUseId"] == "a"

    def test_an_error_result_is_flagged(self, model: BedrockModel) -> None:
        encoded = model.tool_result_message([ToolResult("a", "boom", is_error=True)])
        assert encoded["content"][0]["toolResult"]["status"] == "error"

    def test_a_successful_result_has_no_status(self, model: BedrockModel) -> None:
        encoded = model.tool_result_message([ToolResult("a", "fine")])
        assert "status" not in encoded["content"][0]["toolResult"]

    def test_empty_output_still_says_something(self, model: BedrockModel) -> None:
        encoded = model.tool_result_message([ToolResult("a", "")])
        assert encoded["content"][0]["toolResult"]["content"][0]["text"] == "(no output)"


class TestResponseDecoding:
    def test_plain_text(self) -> None:
        turn = _to_turn(
            {
                "output": {"message": {"content": [{"text": "The answer is 42."}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }
        )
        assert turn.text == "The answer is 42."
        assert turn.tool_calls == []
        assert turn.usage.input_tokens == 10

    def test_tool_use(self) -> None:
        turn = _to_turn(
            {
                "output": {
                    "message": {
                        "content": [
                            {"text": "Looking."},
                            {
                                "toolUse": {
                                    "toolUseId": "tu-1",
                                    "name": "run_query",
                                    "input": {"sql": "SELECT 1"},
                                }
                            },
                        ]
                    }
                },
                "stopReason": "tool_use",
            }
        )
        assert turn.text == "Looking."
        assert turn.tool_calls[0].id == "tu-1"
        assert turn.tool_calls[0].input == {"sql": "SELECT 1"}
        assert turn.stop_reason == "tool_use"

    def test_tool_input_arriving_as_a_json_string(self) -> None:
        """Some models emit the arguments serialised; never string-match them."""
        turn = _to_turn(
            {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "t", "name": "x", "input": '{"a": 1}'}}
                        ]
                    }
                }
            }
        )
        assert turn.tool_calls[0].input == {"a": 1}

    def test_malformed_tool_input_becomes_empty_rather_than_crashing(self) -> None:
        turn = _to_turn(
            {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "t", "name": "x", "input": "{oops"}}
                        ]
                    }
                }
            }
        )
        assert turn.tool_calls[0].input == {}

    def test_assistant_message_is_echoed_verbatim(self) -> None:
        """Tool-use ids must stay paired across the turn boundary."""
        blocks = [{"toolUse": {"toolUseId": "tu-9", "name": "x", "input": {}}}]
        turn = _to_turn({"output": {"message": {"content": blocks}}})
        assert turn.assistant_message == {"role": "assistant", "content": blocks}

    def test_an_empty_response_does_not_crash(self) -> None:
        turn = _to_turn({})
        assert turn.text == ""
        assert turn.tool_calls == []


class TestStreaming:
    def test_rebuilds_the_response_from_a_stream(self) -> None:
        class StreamingClient(StubClient):
            def converse_stream(self, **request: Any) -> dict[str, Any]:
                self.requests.append(request)
                return {
                    "stream": [
                        {"contentBlockDelta": {"delta": {"text": "Check"}}},
                        {"contentBlockDelta": {"delta": {"text": "ing."}}},
                        {"contentBlockStop": {}},
                        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1",
                                                                    "name": "run_query"}}}},
                        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"sql":'}}}},
                        {"contentBlockDelta": {"delta": {"toolUse": {"input": ' "SELECT 1"}'}}}},
                        {"contentBlockStop": {}},
                        {"messageStop": {"stopReason": "tool_use"}},
                        {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 3}}},
                    ]
                }

        streamed: list[str] = []
        model = BedrockModel("m", client=StreamingClient())
        turn = model.generate(system="s", messages=[], tools=[], on_text=streamed.append)

        assert "".join(streamed) == "Checking."
        assert turn.text == "Checking."
        assert turn.tool_calls[0].name == "run_query"
        assert turn.tool_calls[0].input == {"sql": "SELECT 1"}
        assert turn.stop_reason == "tool_use"
        assert turn.usage.output_tokens == 3


class TestFailures:
    def test_a_boto_failure_becomes_a_model_error(self) -> None:
        class Broken:
            def converse(self, **_: Any) -> dict[str, Any]:
                raise RuntimeError("ExpiredTokenException")

        model = BedrockModel("m", client=Broken())
        with pytest.raises(ModelError, match="ExpiredTokenException"):
            model.generate(system="s", messages=[], tools=[])


class TestAgentIntegration:
    def test_drives_the_real_agent_loop(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A Bedrock model must satisfy the Model protocol the loop expects."""
        import agentsuite as lib

        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

        class Scripted(StubClient):
            def __init__(self) -> None:
                super().__init__()
                self.turn = 0

            def converse(self, **request: Any) -> dict[str, Any]:
                self.requests.append(request)
                self.turn += 1
                if self.turn == 1:
                    return {
                        "output": {"message": {"content": [
                            {"toolUse": {"toolUseId": "t1", "name": "list_files",
                                         "input": {"path": "", "pattern": "*.txt"}}}
                        ]}},
                        "stopReason": "tool_use",
                        "usage": {"inputTokens": 5, "outputTokens": 2},
                    }
                return {
                    "output": {"message": {"content": [{"text": "One text file."}]}},
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 8, "outputTokens": 3},
                }

        dev = lib.code(project=tmp_path, model=BedrockModel("amazon.nova-pro-v1:0",
                                                           client=Scripted()))
        result = dev.run("what text files are here?")
        assert result.text == "One text file."
        assert result.turns == 2
        assert "notes.txt" in dev.messages[2]["content"][0]["toolResult"]["content"][0]["text"]
        dev.close()


def test_thinking_support_is_detected_from_the_model_id() -> None:
    assert BedrockModel("us.anthropic.claude-sonnet-4-5", client=StubClient()).supports_thinking
    assert not BedrockModel("amazon.nova-pro-v1:0", client=StubClient()).supports_thinking


def test_tool_spec_defaults_a_missing_schema() -> None:
    assert _tool_spec({"name": "x"})["toolSpec"]["inputSchema"]["json"] == {"type": "object"}
