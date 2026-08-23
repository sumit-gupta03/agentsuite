"""Choosing a language model provider.

One string selects the backend, so the same code runs against whichever provider
the person using it actually has::

    agent.pyspark(project="./etl", model="claude-opus-5")
    agent.pyspark(project="./etl", model="bedrock:amazon.nova-pro-v1:0")
    agent.pyspark(project="./etl", model="openai:gpt-4o")

Or from configuration, with no code change at all::

    AGENT_MODEL=bedrock:amazon.nova-pro-v1:0

``provider:model-id`` is always unambiguous. A bare id is matched against known
naming conventions, and an unrecognised one is assumed to be Anthropic rather
than guessed at.

Nothing here reads a credential. Each backend uses its provider's ordinary
resolution -- environment variables, a shared AWS profile, an instance role --
so a key never passes through this library.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .errors import ConfigError
from .model import Model

#: provider -> (import path, pip extra, underlying SDK module, description)
#:
#: The SDK module is named separately because each backend imports its SDK
#: lazily -- so importing the backend proves nothing about whether the SDK is
#: actually installed, and reporting it as available would be a lie.
PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "anthropic": (
        "agentkart.core.model:ClaudeModel",
        "",
        "anthropic",
        "Claude via the Anthropic API. Needs ANTHROPIC_API_KEY or an `ant auth login` profile.",
    ),
    "bedrock": (
        "agentkart.core.bedrock:BedrockModel",
        "bedrock",
        "boto3",
        "Amazon Nova, Claude, Llama and others via Bedrock. Uses your AWS credential chain.",
    ),
    "openai": (
        "agentkart.core.openai_model:OpenAIModel",
        "openai",
        "openai",
        "GPT models, and any OpenAI-compatible endpoint. Needs OPENAI_API_KEY.",
    ),
}

#: Aliases people reach for.
ALIASES = {
    "claude": "anthropic",
    "aws": "bedrock",
    "nova": "bedrock",
    "azure": "openai",
    "gpt": "openai",
}

#: Patterns that identify a provider from a bare model id.
_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"^claude[-.]", "anthropic"),
    (r"^(?:us|eu|apac)\.anthropic\.", "bedrock"),
    (r"^(?:amazon|anthropic|meta|mistral|cohere|ai21|deepseek)\.", "bedrock"),
    (r"^(?:us|eu|apac)\.(?:amazon|meta|mistral)\.", "bedrock"),
    (r"^(?:gpt|o[1-9]|chatgpt|text-davinci)", "openai"),
)

#: A provider prefix is a bare word -- no dots, which model ids always have.
_PROVIDER_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Used when a model id names no provider and matches no signature.
DEFAULT_PROVIDER = "anthropic"


def split_spec(spec: str) -> tuple[str, str]:
    """Split ``"provider:model-id"`` into its parts, inferring the provider.

    A bare id keeps its full text as the model id -- Bedrock ids contain colons
    (``amazon.nova-pro-v1:0``), so only a *known* provider prefix is treated as
    one.
    """
    text = spec.strip()
    if not text:
        raise ConfigError("model must be a non-empty string")

    head, sep, tail = text.partition(":")
    candidate = ALIASES.get(head.lower(), head.lower())
    if sep and candidate in PROVIDERS and tail:
        return candidate, tail.strip()

    # A colon is ambiguous: "openai:gpt-4o" names a provider, but a Bedrock id
    # ends in one ("amazon.nova-pro-v1:0"). Only a bare word followed by
    # something that is not a version number reads as a provider prefix -- and
    # if it does, an unknown one is a typo worth reporting rather than silently
    # routing to the default.
    if sep and tail and _PROVIDER_PREFIX.match(head) and not tail.isdigit():
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigError(
            f"unknown model provider {head!r} in {spec!r}. Available: {known}. "
            "For a bare model id containing a colon, omit the prefix."
        )

    for pattern, provider in _SIGNATURES:
        if re.match(pattern, text, re.IGNORECASE):
            return provider, text
    return DEFAULT_PROVIDER, text


def load_backend(provider: str) -> Callable[..., Model]:
    """Import the backend class for ``provider``."""
    key = ALIASES.get(provider.lower(), provider.lower())
    if key not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigError(f"unknown model provider {provider!r}. Available: {known}")

    target, extra, _sdk, _about = PROVIDERS[key]
    module_path, _, class_name = target.partition(":")
    try:
        from importlib import import_module

        module = import_module(module_path)
    except ImportError as exc:
        hint = f' Install it with: pip install "agentkart[{extra}]"' if extra else ""
        raise ConfigError(f"provider {key!r} could not be loaded: {exc}.{hint}") from exc
    backend: Callable[..., Model] = getattr(module, class_name)
    return backend


def resolve_model(spec: str | Model | None, **options: Any) -> Model:
    """Build a model from a spec string, or pass a constructed one through.

    Args:
        spec: ``"provider:model-id"``, a bare model id, an already-built
            :class:`~agentkart.core.model.Model`, or ``None`` for the default.
        **options: Forwarded to the backend -- ``max_tokens``, ``region``,
            ``api_key``, ``base_url``, and so on. Anything the chosen backend
            does not accept is dropped rather than raising, so one config can
            carry settings for several providers.
    """
    if spec is not None and not isinstance(spec, str):
        return spec

    from .model import DEFAULT_MODEL

    provider, model_id = split_spec(spec or DEFAULT_MODEL)
    backend = load_backend(provider)

    import inspect

    # Only options the backend *names* are forwarded. Several backends accept
    # **kwargs to pass through to their SDK client, so matching on that would
    # hand Anthropic's `effort` to boto3 and fail deep inside the provider.
    accepted = {
        name
        for name, parameter in inspect.signature(backend).parameters.items()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    usable = {k: v for k, v in options.items() if v is not None and k in accepted}
    return backend(model_id, **usable)


def describe_providers() -> list[tuple[str, str, str]]:
    """``(name, extra, description)`` for every provider, installed or not."""
    return [
        (name, extra, about) for name, (_, extra, _sdk, about) in sorted(PROVIDERS.items())
    ]


def available_providers() -> dict[str, bool]:
    """Which providers have their SDK installed in this environment.

    Checks for the SDK itself rather than the backend module -- the backends
    import their SDK lazily, so importing one proves nothing.
    """
    from importlib.util import find_spec

    status: dict[str, bool] = {}
    for name, (_target, _extra, sdk, _about) in PROVIDERS.items():
        try:
            status[name] = find_spec(sdk) is not None
        except (ImportError, ValueError):  # pragma: no cover - odd install
            status[name] = False
    return status


__all__ = [
    "ALIASES",
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "available_providers",
    "describe_providers",
    "load_backend",
    "resolve_model",
    "split_spec",
]
