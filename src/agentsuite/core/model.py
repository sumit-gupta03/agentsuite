"""The language-model backend.

The agent loop lives in :mod:`agent.agent`, *above* this abstraction,
which is the deliberate choice that lets a different provider be dropped in
without reimplementing guardrails, skills or tool dispatch. A backend owns three
things and nothing else: how a turn is generated, and how user messages and tool
results are encoded for its own wire format.

Claude is the only first-class implementation. That is not an oversight -- tool
calling, prompt caching and thinking semantics differ enough between providers
that a lowest-common-denominator abstraction would make every backend worse.
Implement :class:`Model` to add your own.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from .errors import ModelError, RefusalError
from .types import ModelTurn, ToolCall, ToolResult, Usage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16_000

#: Server-side fallback keeps a run alive when a safety classifier declines a
#: turn. Data engineering prompts trip these rarely, but a nightly job that dies
#: on one refusal is a worse outcome than one that routes around it.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@runtime_checkable
class Model(Protocol):
    """What the agent loop requires of a language-model backend."""

    model_id: str

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> ModelTurn:
        """Produce one assistant turn."""
        ...

    def user_message(self, text: str) -> dict[str, Any]:
        """Encode a user turn for this provider's transcript format."""
        ...

    def tool_result_message(self, results: Sequence[ToolResult]) -> dict[str, Any]:
        """Encode a batch of tool results as a single transcript entry."""
        ...


class ClaudeModel:
    """Claude backend built on the Anthropic SDK.

    Defaults worth knowing about:

    * **Adaptive thinking** is on. Data engineering questions are exactly the
      kind of multi-constraint reasoning it helps with.
    * **Prompt caching** is applied to the system prompt and tool definitions.
      Those are the stable prefix of every turn in a session, and the skill
      index makes them large, so caching is the difference between a cheap
      twenty-turn run and an expensive one.
    * **Refusal fallback** is enabled. Pass ``refusal_fallback=False`` to use the
      plain (non-beta) endpoint instead.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = "high",
        thinking: bool = True,
        cache_prompt: bool = True,
        refusal_fallback: bool = True,
        client: Any | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.effort = effort
        self.thinking = thinking
        self.cache_prompt = cache_prompt
        self.refusal_fallback = refusal_fallback

        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ModelError(
                    "the anthropic SDK is not installed. Install it with: pip install anthropic"
                ) from exc
            # A bare constructor also picks up an OAuth profile from `ant auth
            # login`, so an unset ANTHROPIC_API_KEY is not necessarily an error.
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            self._client = (
                anthropic.Anthropic(api_key=key, **client_kwargs)
                if key
                else anthropic.Anthropic(**client_kwargs)
            )

    # -- transcript encoding ------------------------------------------------

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def tool_result_message(self, results: Sequence[ToolResult]) -> dict[str, Any]:
        # All results for one assistant turn go in a single user message.
        # Splitting them across messages teaches the model to stop calling tools
        # in parallel, which costs turns for the rest of the session.
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content or "(no output)",
                    **({"is_error": True} if result.is_error else {}),
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
        kwargs = self._build_kwargs(system=system, messages=messages, tools=tools)
        message = self._stream(kwargs, on_text)

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise RefusalError(
                "the model declined this request"
                + (f" (category: {category})" if category else "")
                + ". Rephrase the task, or narrow it to the specific data operation you need.",
                category=category,
            )

        return self._to_turn(message)

    def _build_kwargs(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Render order is tools -> system -> messages, so the cache breakpoint
        # goes on the last system block to cover both stable sections.
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if self.cache_prompt:
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.refusal_fallback:
            kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _stream(self, kwargs: dict[str, Any], on_text: Callable[[str], None] | None) -> Any:
        """Stream a turn, degrading gracefully on older SDK builds.

        Streaming is not optional here: a twenty-turn agent run with a large
        ``max_tokens`` will otherwise hit HTTP read timeouts.
        """
        beta = "betas" in kwargs
        for attempt in range(2):
            endpoint = self._client.beta.messages if beta else self._client.messages
            try:
                with endpoint.stream(**kwargs) as stream:
                    if on_text is not None:
                        for chunk in stream.text_stream:
                            on_text(chunk)
                    return stream.get_final_message()
            except TypeError as exc:
                # An older SDK that does not know a newer parameter name. Move
                # the modern fields into extra_body and try once more.
                if attempt == 1:
                    raise ModelError(
                        f"the installed anthropic SDK rejected a parameter: {exc}"
                    ) from exc
                kwargs = self._downgrade(kwargs)
                beta = "betas" in kwargs
                logger.debug("retrying with extra_body after TypeError: %s", exc)
            except Exception as exc:  # noqa: BLE001 - normalise provider errors
                raise ModelError(f"model request failed: {exc}") from exc
        raise ModelError("model request failed after retrying")  # pragma: no cover

    @staticmethod
    def _downgrade(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Move newer top-level parameters into ``extra_body``."""
        downgraded = dict(kwargs)
        extra: dict[str, Any] = dict(downgraded.pop("extra_body", {}) or {})
        for field in ("output_config", "fallbacks", "thinking", "speed"):
            if field in downgraded:
                extra[field] = downgraded.pop(field)
        # `betas` only exists on the beta endpoint; without it, drop back to the
        # stable one and carry the header manually.
        betas = downgraded.pop("betas", None)
        if betas:
            headers = dict(downgraded.pop("extra_headers", {}) or {})
            headers["anthropic-beta"] = ",".join(betas)
            downgraded["extra_headers"] = headers
        if extra:
            downgraded["extra_body"] = extra
        return downgraded

    @staticmethod
    def _to_turn(message: Any) -> ModelTurn:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in message.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                raw_input = block.input
                if isinstance(raw_input, str):
                    # Defensive: tool input must always be parsed as JSON rather
                    # than string-matched, since escaping varies by model.
                    try:
                        raw_input = json.loads(raw_input)
                    except json.JSONDecodeError:
                        raw_input = {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=raw_input or {}))

        usage_obj = getattr(message, "usage", None)
        usage = Usage(
            input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        )

        try:
            content = message.model_dump(mode="json", exclude_none=True)["content"]
        except Exception:  # noqa: BLE001 - non-pydantic stand-ins in tests
            content = message.content

        return ModelTurn(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop_reason=getattr(message, "stop_reason", None),
            usage=usage,
            assistant_message={"role": "assistant", "content": content},
        )


__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_MODEL", "ClaudeModel", "Model"]
