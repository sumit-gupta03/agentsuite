"""OpenAI backend, via chat completions with tool calling.

::

    import agentsuite as agent
    dev = agent.pyspark(project="./etl", model="openai:gpt-4o")

Credentials come from ``OPENAI_API_KEY`` or the SDK's own resolution. This module
never takes a key positionally, so one cannot end up in a traceback.

Requires the OpenAI SDK: ``pip install "agentsuite[openai]"``.

Two shape differences from the other backends, both handled here so the agent
loop never learns about them:

* the system prompt is a **message**, not a separate field;
* each tool result is its own ``role: "tool"`` message rather than one batched
  user turn. The loop still hands over a batch; this module splits it.

**Not exercised against the live API.** The encoding is unit-tested with a stub
client; the wire is not.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from .errors import ModelError
from .types import ModelTurn, ToolCall, ToolResult, Usage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_TOKENS = 8_000

#: Marks the batched tool-result envelope the loop produces, so it can be
#: flattened back into individual messages at request time.
_BATCH = "__tool_results__"


class OpenAIModel:
    """A :class:`~agentsuite.core.model.Model` backed by OpenAI chat completions."""

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
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
            import openai
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ModelError(
                'the openai package is not installed. Install it with: '
                'pip install "agentsuite[openai]"'
            ) from exc

        if base_url:
            client_kwargs["base_url"] = base_url
        if api_key:
            client_kwargs["api_key"] = api_key
        self._client = openai.OpenAI(**client_kwargs)

    # -- transcript encoding ------------------------------------------------

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_result_message(self, results: Sequence[ToolResult]) -> dict[str, Any]:
        """One envelope carrying every result, flattened at request time.

        The loop's contract is one transcript entry per batch. OpenAI wants a
        separate ``role: "tool"`` message each, so the split happens in
        :meth:`_flatten` rather than leaking into the loop.
        """
        return {
            "role": _BATCH,
            "results": [
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content or "(no output)",
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
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, *_flatten(messages)],
            "max_completion_tokens": self.max_tokens,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if tools:
            request["tools"] = [_function_spec(t) for t in tools]

        try:
            completion = self._client.chat.completions.create(**request)
        except TypeError as exc:
            # Older SDKs and compatible gateways still use max_tokens.
            if "max_completion_tokens" not in str(exc):
                raise ModelError(f"openai request failed: {exc}") from exc
            request["max_tokens"] = request.pop("max_completion_tokens")
            try:
                completion = self._client.chat.completions.create(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise ModelError(f"openai request failed: {retry_exc}") from retry_exc
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise ModelError(f"openai request failed: {exc}") from exc

        turn = _to_turn(completion)
        if on_text is not None and turn.text:
            on_text(turn.text)
        return turn

    def __repr__(self) -> str:
        return f"<OpenAIModel {self.model_id}>"


# -- translation -----------------------------------------------------------


def _flatten(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand batched tool-result envelopes into individual messages."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == _BATCH:
            out.extend(message.get("results", []))
        else:
            out.append(message)
    return out


def _function_spec(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic-shaped tool definition into an OpenAI function."""
    schema = dict(tool.get("input_schema", {"type": "object"}))
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        },
    }


def _to_turn(completion: Any) -> ModelTurn:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise ModelError("openai returned no choices")
    message = choices[0].message

    text = getattr(message, "content", None) or ""
    tool_calls: list[ToolCall] = []
    raw_calls = getattr(message, "tool_calls", None) or []

    for call in raw_calls:
        function = getattr(call, "function", None)
        if function is None:
            continue
        # Arguments arrive as a JSON string. Always parse; never string-match.
        arguments = getattr(function, "arguments", "") or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            logger.warning("openai tool arguments were not valid JSON: %.120s", arguments)
            parsed = {}
        tool_calls.append(
            ToolCall(
                id=str(getattr(call, "id", "")),
                name=str(getattr(function, "name", "")),
                input=parsed if isinstance(parsed, dict) else {},
            )
        )

    raw_usage = getattr(completion, "usage", None)
    usage = Usage(
        input_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
    )

    assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
    if raw_calls:
        assistant["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in raw_calls
        ]

    return ModelTurn(
        text=text.strip(),
        tool_calls=tool_calls,
        stop_reason=str(getattr(choices[0], "finish_reason", "")) or None,
        usage=usage,
        assistant_message=assistant,
    )


__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_MODEL", "OpenAIModel"]
