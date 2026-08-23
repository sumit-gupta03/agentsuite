"""agent -- one core, many domains.

::

    import agentkart as agent

    de = agent.dataengineering(warehouse="snowflake")
    sf = agent.snowflake()                       # a preset over the same domain

    de.run("Profile raw.orders and flag anything that breaks a join")

**Importing this package does nothing.** It opens no connections, reads no
credentials and contacts no API. Domains and core names resolve lazily on first
attribute access, so the cost of ``import agent`` does not grow as domains are
added. Work starts when you build an agent -- explicitly, where you can see it.

Two kinds of thing hang off this namespace:

**Domains** bring tools, a permission policy and a skill library. They are the
expensive thing to add, and there should be few of them::

    agent.dataengineering(...)

**Presets** are a domain plus configuration plus skills, and cost no code::

    agent.snowflake()   ==  agent.dataengineering(warehouse="snowflake")

Reach for a preset first. Add a domain only when the tools genuinely differ --
when there is something the existing tools cannot do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

# Core names, resolved on first access (PEP 562). name -> submodule.
_CORE: dict[str, str] = {
    "Agent": ".core.loop",
    "AgentContext": ".core.loop",
    "ActionRecord": ".core.loop",
    "ConfirmFn": ".core.loop",
    "confirm_in_terminal": ".core.loop",
    "deny_all": ".core.loop",
    "Config": ".core.config",
    "load_config": ".core.config",
    "Domain": ".core.domain",
    "register_domain": ".core.domain",
    "BedrockModel": ".core.bedrock",
    "ClaudeModel": ".core.model",
    "OpenAIModel": ".core.openai_model",
    "resolve_model": ".core.providers",
    "Model": ".core.model",
    "Action": ".core.policy",
    "Policy": ".core.policy",
    "Tier": ".core.policy",
    "Verdict": ".core.policy",
    "Router": ".core.router",
    "RoutingDecision": ".core.router",
    "EmbeddingSelector": ".core.retrieval",
    "KeywordSelector": ".core.retrieval",
    "SkillSelector": ".core.retrieval",
    "VectorStore": ".core.retrieval",
    "Skill": ".core.skills",
    "resolve_skills": ".core.skills",
    "ToolRegistry": ".core.tools",
    "ToolSpec": ".core.tools",
    "tool": ".core.tools",
    "RunResult": ".core.types",
    "ToolCall": ".core.types",
    "ToolResult": ".core.types",
    "Usage": ".core.types",
    "AgentError": ".core.errors",
    "ConfigError": ".core.errors",
    "MaxTurnsExceeded": ".core.errors",
    "ModelError": ".core.errors",
    "RefusalError": ".core.errors",
    "SkillError": ".core.errors",
    "ToolError": ".core.errors",
}

if TYPE_CHECKING:  # pragma: no cover - for type checkers and IDEs only
    from .core.bedrock import BedrockModel
    from .core.config import Config, load_config
    from .core.domain import Domain
    from .core.domain import register as register_domain
    from .core.errors import (
        AgentError,
        ConfigError,
        MaxTurnsExceeded,
        ModelError,
        RefusalError,
        SkillError,
        ToolError,
    )
    from .core.loop import (
        ActionRecord,
        Agent,
        AgentContext,
        ConfirmFn,
        confirm_in_terminal,
        deny_all,
    )
    from .core.model import ClaudeModel, Model
    from .core.openai_model import OpenAIModel
    from .core.policy import Action, Policy, Tier, Verdict
    from .core.providers import resolve_model
    from .core.retrieval import (
        EmbeddingSelector,
        KeywordSelector,
        SkillSelector,
        VectorStore,
    )
    from .core.router import Router, RoutingDecision
    from .core.skills import Skill, resolve_skills
    from .core.tools import ToolRegistry, ToolSpec, tool
    from .core.types import RunResult, ToolCall, ToolResult, Usage


def __getattr__(name: str) -> Any:
    """Resolve a core name, a domain, or a preset on first access."""
    from importlib import import_module

    module_name = _CORE.get(name)
    if module_name is not None:
        module = import_module(module_name, __name__)
        attribute = "register" if name == "register_domain" else name
        value = getattr(module, attribute)
        globals()[name] = value
        return value

    factory = _domain_factory(name)
    if factory is not None:
        globals()[name] = factory
        return factory

    raise AttributeError(f"module 'agentkart' has no attribute {name!r}")


def _domain_factory(name: str) -> Any:
    """Return a factory for a domain name, or for a preset of some domain."""
    from functools import partial

    from .core import domain as domain_module
    from .core.errors import ConfigError

    # A domain by name or alias.
    try:
        found = domain_module.get(name)
    except ConfigError:
        found = None

    if found is not None:
        factory = _factory_for(found)
        factory.__name__ = name
        factory.__doc__ = f"Build a {found.name} agent -- {found.description}."
        return factory

    # A preset of some domain: agent.snowflake() -> dataengineering(preset=...)
    for domain_name in domain_module.names():
        try:
            candidate = domain_module.get(domain_name)
        except ConfigError:  # pragma: no cover - broken plugin
            continue
        if name in candidate.presets:
            factory = partial(_factory_for(candidate), preset=name)
            factory.__doc__ = (
                f"Build a {candidate.name} agent preset for {name!r}: "
                f"{candidate.presets[name]}."
            )
            return factory
    return None


def _factory_for(found: Any) -> Any:
    from importlib import import_module

    module = import_module(found.package)
    factory = getattr(module, "create", None)
    if factory is None:  # pragma: no cover - malformed domain package
        from .core.errors import ConfigError

        raise ConfigError(f"domain {found.name!r} does not expose a create() factory")
    return factory


def list_domains() -> list[tuple[str, str]]:
    """Every installed domain as ``(name, description)``.

    Named ``list_domains`` rather than ``domains`` because ``agentkart.domains`` is
    the subpackage holding them -- importing it would shadow a function of that
    name.
    """
    from .core.domain import REGISTRY

    return REGISTRY.describe()


def list_providers() -> list[tuple[str, str, bool]]:
    """Model providers, as ``(name, description, installed)``.

    ::

        for name, about, installed in agent.list_providers():
            print("x" if installed else " ", name, about)

    Use whichever you have. The same agent code runs against any of them.
    """
    from .core.providers import available_providers, describe_providers

    installed = available_providers()
    return [(name, about, installed.get(name, False)) for name, _, about in describe_providers()]


def list_presets() -> dict[str, list[str]]:
    """Every preset, grouped by the domain that provides it."""
    from .core import domain as domain_module
    from .core.errors import ConfigError

    out: dict[str, list[str]] = {}
    for name in domain_module.names():
        try:
            found = domain_module.get(name)
        except ConfigError:  # pragma: no cover - broken plugin
            continue
        if found.presets and found.name not in out:
            out[found.name] = sorted(found.presets)
    return out


def __dir__() -> list[str]:
    names = [
        *_CORE,
        "auto",
        "list_domains",
        "list_presets",
        "list_providers",
        "start",
        "__version__",
    ]
    try:
        from .core import domain as domain_module

        for name in domain_module.names():
            names.append(name)
            try:
                names.extend(domain_module.get(name).presets)
            except Exception:  # noqa: BLE001 - dir() must never raise
                pass
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(names))


def auto(**session: Any) -> Any:
    """Route plain English to the right agent, then run it.

    ::

        import agentkart as agent

        session = agent.auto(project="./etl", warehouse="snowflake")
        session.run("the nightly spark job skews on customer_id")   # -> pyspark
        session.run("write tests for the new parser")               # -> testing

    Everything passed here -- ``project``, ``warehouse``, ``write``, ``confirm``,
    ``audit_path`` -- is fixed for every route. **The prompt chooses the
    specialism; it never chooses the permissions.** That separation is what makes
    it safe to route text the agent did not author.

    ``model`` is used both to classify the request and by every agent the router
    builds. Pass ``routing_model`` to classify with something cheaper than the
    model that does the work.

    Reserved keyword arguments: ``presets``, ``fallback``, ``routing_model``,
    ``on_route``. Everything else -- ``model`` included -- is session
    configuration passed to each agent.
    """
    from .core.router import Router

    # `model` stays in the session so the delegated agents use it, and doubles as
    # the classifier unless a separate routing_model is supplied.
    routing_model = session.pop("routing_model", None) or session.get("model")

    return Router(
        presets=tuple(session.pop("presets", ()) or ()),
        fallback=session.pop("fallback", "python"),
        model=routing_model if not isinstance(routing_model, str) else None,
        on_route=session.pop("on_route", None),
        session=session,
    )


def start(domain: str = "dataengineering", **kwargs: Any) -> None:
    """Open an interactive session in the terminal.

    Convenience for exploratory work::

        import agentkart as agent
        agent.start("dataengineering", warehouse="duckdb")

    For anything scripted, build the agent directly -- you get the transcript,
    the audit log and the return values.
    """
    from .cli import interactive

    interactive(domain=domain, **kwargs)


__all__ = [
    *sorted(_CORE),
    "__version__",
    "auto",
    "list_domains",
    "list_presets",
    "list_providers",
    "start",
]
