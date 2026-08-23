"""Amazon Bedrock backend, via the Converse API.

One implementation covers every Bedrock model that supports tool use -- Amazon
Nova, Anthropic Claude on Bedrock, Mistral, Llama -- because Converse gives them
all the same request and response shape.

::

    import agentkart as agent

    dev = agent.pyspark(
        project="./etl",
        model=agent.BedrockModel("amazon.nova-pro-v1:0", region="us-east-1"),
    )

Credentials come from the ordinary boto3 chain: environment variables, a shared
profile, an instance role. This module never takes a key as an argument, so a
key cannot end up in a traceback or a notebook.

Requires boto3: ``pip install "agentkart[bedrock]"``.

**This backend has not been exercised against a live Bedrock endpoint.** It is
written against the documented Converse shape and its encoding is unit-tested
with a stub client, but the wire is unverified. Treat the first live run as the
test.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from .errors import ModelError
from .types import ModelTurn, ToolCall, ToolResult, Usage

logger = logging.getLogger(__name__)

#: A sensible default that supports tool use. Override per session.
DEFAULT_MODEL = "amazon.nova-pro-v1:0"
#: Under the lowest per-model cap on Bedrock (Nova Pro allows 10k), so an
#: unconfigured session works on every model rather than only the generous ones.
DEFAULT_MAX_TOKENS = 8_000

#: Bedrock model id prefixes that accept Anthropic-style extended thinking.
_THINKING_PREFIXES = ("anthropic.", "us.anthropic.", "eu.anthropic.")


class BedrockModel:
    """A :class:`~agentkart.core.model.Model` backed by Bedrock's Converse API.

    Args:
        model_id: A Bedrock model id or inference profile ARN, e.g.
            ``"amazon.nova-pro-v1:0"`` or ``"us.anthropic.claude-sonnet-4-5-20250929-v1:0"``.
        region: AWS region. Falls back to the boto3 session default.
        profile: Named AWS profile, for local development.
        client: A pre-built ``bedrock-runtime`` client, mainly for testing.
        temperature: Passed through when set; Converse defaults it otherwise.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        *,
        region: str | None = None,
        profile: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        client: Any | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature

        if client is not None:
            self._client = client
            return

        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ModelError(
                'boto3 is not installed. Install it with: pip install "agentkart[bedrock]"'
            ) from exc

        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        resolved_region = region or session.region_name
        if not resolved_region:
            raise ModelError(
                "no AWS region for Bedrock. Pass region=..., set AWS_REGION, or put a "
                "region in your AWS profile. Without one botocore fails several frames "
                "deep with an error that says nothing about agentkart."
            )
        try:
            self._client = session.client(
                "bedrock-runtime", region_name=resolved_region, **client_kwargs
            )
        except Exception as exc:  # noqa: BLE001 - normalise botocore's setup errors
            raise ModelError(f"could not create a Bedrock client: {exc}") from exc

    # -- transcript encoding ------------------------------------------------

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": [{"text": text}]}

    def tool_result_message(self, results: Sequence[ToolResult]) -> dict[str, Any]:
        # All results for one assistant turn go in a single user message, as with
        # every other backend -- splitting them teaches the model to stop calling
        # tools in parallel.
        return {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": result.call_id,
                        "content": [{"text": result.content or "(no output)"}],
                        **({"status": "error"} if result.is_error else {}),
                    }
                }
                for result in results
            ],
        }

    # -- generation ---------------------------------------------------------

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> ModelTurn:
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "system": [{"text": system}],
            "inferenceConfig": {"maxTokens": self.max_tokens},
        }
        if self.temperature is not None:
            request["inferenceConfig"]["temperature"] = self.temperature
        if tools:
            request["toolConfig"] = {"tools": [_tool_spec(t) for t in tools]}

        try:
            if on_text is not None and hasattr(self._client, "converse_stream"):
                response = self._stream(request, on_text)
            else:
                response = self._client.converse(**request)
        except Exception as exc:  # noqa: BLE001 - normalise every boto failure
            raise ModelError(f"bedrock converse failed: {exc}") from exc

        return _to_turn(response)

    def _stream(self, request: dict[str, Any], on_text: Callable[[str], None]) -> dict[str, Any]:
        """Consume a Converse stream and rebuild the non-streaming response shape.

        Streaming matters for the same reason it does elsewhere: a long agent turn
        with a large ``maxTokens`` will otherwise sit on an idle socket.
        """
        stream = self._client.converse_stream(**request)

        content: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        tool_input = ""
        stop_reason = "end_turn"
        usage: dict[str, Any] = {}

        for event in stream.get("stream", []):
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    current = {"toolUse": dict(start["toolUse"])}
                    tool_input = ""
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    on_text(delta["text"])
                    if current is None or "text" not in current:
                        current = {"text": ""}
                    current["text"] = current.get("text", "") + delta["text"]
                elif "toolUse" in delta:
                    tool_input += delta["toolUse"].get("input", "")
            elif "contentBlockStop" in event:
                if current is not None:
                    if "toolUse" in current:
                        current["toolUse"]["input"] = _parse_json(tool_input)
                    content.append(current)
                    current = None
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", "end_turn")
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})

        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop_reason,
            "usage": usage,
        }

    @property
    def supports_thinking(self) -> bool:
        """Whether this model id accepts Anthropic-style extended thinking."""
        return self.model_id.startswith(_THINKING_PREFIXES)

    def __repr__(self) -> str:
        return f"<BedrockModel {self.model_id}>"


# -- translation -----------------------------------------------------------


def _tool_spec(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic-shaped tool definition into a Converse toolSpec.

    The registry emits one shape; each backend translates. That keeps tool
    authors writing a single definition rather than one per provider.
    """
    return {
        "toolSpec": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": {"json": tool.get("input_schema", {"type": "object"})},
        }
    }


def _to_turn(response: dict[str, Any]) -> ModelTurn:
    message = response.get("output", {}).get("message", {}) or {}
    blocks = message.get("content", []) or []

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in blocks:
        if "text" in block:
            text_parts.append(str(block["text"]))
        elif "toolUse" in block:
            use = block["toolUse"]
            raw = use.get("input", {})
            # Defensive: some models emit the arguments as a JSON string.
            if isinstance(raw, str):
                raw = _parse_json(raw)
            tool_calls.append(
                ToolCall(
                    id=str(use.get("toolUseId", "")),
                    name=str(use.get("name", "")),
                    input=raw if isinstance(raw, dict) else {},
                )
            )

    raw_usage = response.get("usage", {}) or {}
    usage = Usage(
        input_tokens=int(raw_usage.get("inputTokens", 0) or 0),
        output_tokens=int(raw_usage.get("outputTokens", 0) or 0),
        cache_read_tokens=int(raw_usage.get("cacheReadInputTokens", 0) or 0),
        cache_write_tokens=int(raw_usage.get("cacheWriteInputTokens", 0) or 0),
    )

    return ModelTurn(
        text="".join(text_parts).strip(),
        tool_calls=tool_calls,
        stop_reason=str(response.get("stopReason", "")) or None,
        usage=usage,
        # Echoed back verbatim on the next turn, so tool_use ids stay paired.
        assistant_message={"role": "assistant", "content": blocks},
    )


def _parse_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("bedrock tool input was not valid JSON: %.120s", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_MODEL", "BedrockModel"]
