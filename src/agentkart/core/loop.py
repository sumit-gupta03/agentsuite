"""The agent loop.

Domain-agnostic by construction: this module knows about skills, tools, a policy
and a model, and nothing about SQL, warehouses, source files or clouds. Adding a
domain must never require editing this file -- if it does, the abstraction is
wrong and :class:`~agentkart.core.domain.Domain` should grow a hook instead.

The loop itself is about forty lines. That is not where the value is. The value
is in the skill library it assembles, the policy every action passes through, and
the provenance it can show you afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog, RunManifest, summarise_skills
from .config import Config, load_config
from .domain import Domain
from .errors import ConfirmationDenied, MaxTurnsExceeded, ToolError
from .model import Model
from .policy import Policy
from .prompts import build_system_prompt
from .providers import resolve_model
from .retrieval import SkillSelector, describe_selection
from .skills import Skill, resolve_skills
from .tools import ToolRegistry, ToolSpec
from .types import RunResult, ToolCall, ToolResult, Usage
from .untrusted import SYSTEM_RULE, new_nonce, wrap_tool_result

logger = logging.getLogger(__name__)

#: Signature of the confirmation callback: (action, detail, purpose) -> approved.
ConfirmFn = Callable[[str, str, str], bool]


def deny_all(action: str, detail: str, purpose: str) -> bool:
    """Default confirmation policy: refuse everything destructive.

    A library that silently approves destructive operations because nobody
    supplied a callback is a library that eventually destroys something.
    """
    logger.warning("destructive action refused (no confirmation handler): %s", action)
    return False


def confirm_in_terminal(action: str, detail: str, purpose: str) -> bool:
    """Interactive confirmation for terminal sessions."""
    print("\n" + "=" * 68)
    print(f"  CONFIRM DESTRUCTIVE ACTION: {action}")
    if purpose:
        print(f"  Purpose: {purpose}")
    print("-" * 68)
    print(detail)
    print("=" * 68)
    try:
        answer = input("  Proceed? Type 'yes' to allow: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "yes"


@dataclass
class ActionRecord:
    """One action the agent performed, for the audit log."""

    detail: str
    tier: str
    purpose: str = ""
    kind: str = ""


@dataclass
class AgentContext:
    """What tools receive as their first argument.

    Tools get exactly this and nothing more -- no agent, no model, no API client.
    That boundary is what stops a tool reaching around the policy layer.

    ``connection`` is whatever the domain established: a warehouse, a project
    directory, a cloud session. Domains add convenience accessors to their own
    context subclass rather than widening this one.
    """

    policy: Policy
    skills: dict[str, Skill]
    confirm_fn: ConfirmFn = deny_all
    connection: Any = None
    config: Config | None = None
    capabilities: set[str] = field(default_factory=set)
    actions: list[ActionRecord] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    audit: AuditLog = field(default_factory=AuditLog)
    #: Per-run token used to fence untrusted content. Payloads cannot guess it.
    nonce: str = field(default_factory=new_nonce)

    def __post_init__(self) -> None:
        """Hook for domain context subclasses. The base needs nothing."""

    def confirm(self, *, action: str, detail: str, purpose: str = "") -> bool:
        approved = bool(self.confirm_fn(action, detail, purpose))
        self.audit.record(
            "confirmation",
            detail=f"{action}: {detail}",
            purpose=purpose,
            outcome="approved" if approved else "denied",
            tier="destructive",
        )
        return approved

    def record(self, detail: str, tier: str, purpose: str = "", kind: str = "") -> None:
        self.actions.append(ActionRecord(detail=detail, tier=tier, purpose=purpose, kind=kind))
        self.audit.record(
            "action", detail=detail, tier=tier, purpose=purpose, outcome="performed", source=kind
        )

    def record_skill_use(self, name: str) -> None:
        if name not in self.skills_used:
            self.skills_used.append(name)
        self.audit.record("skill_loaded", detail=name, outcome="loaded")


class Agent:
    """A skill-driven agent, specialised by its domain.

    Usually built through a domain factory rather than directly::

        import agentkart as agent
        de = agent.dataengineering(warehouse="duckdb")

    Construct this class directly when assembling something custom from your own
    skills and tools.
    """

    def __init__(
        self,
        *,
        domain: Domain | None = None,
        model: str | Model | None = None,
        profile: str | None = None,
        config: Config | None = None,
        connection: Any = None,
        policy: Policy | None = None,
        tools: Iterable[ToolSpec] = (),
        skills: str | Path | Sequence[str | Path] = (),
        disable_skills: Iterable[str] = (),
        allow_plugins: bool | None = None,
        write: bool | None = None,
        max_turns: int | None = None,
        confirm: ConfirmFn | None = None,
        audit: AuditLog | None = None,
        audit_path: str | Path | None = None,
        mcp_servers: dict[str, Any] | None = None,
        skill_selector: SkillSelector | None = None,
        system_prompt_extra: str = "",
        api_key: str | None = None,
        context_class: type[AgentContext] = AgentContext,
        **domain_options: Any,
    ) -> None:
        if isinstance(skills, (str, Path)):
            skills = [skills]
        skill_dirs = [str(p) for p in skills]

        self.domain = domain
        self.config = config or load_config(
            profile,
            model=model if isinstance(model, str) else None,
            write=write,
            max_turns=max_turns,
            allow_plugins=allow_plugins,
            skill_dirs=skill_dirs or None,
            disable_skills=list(disable_skills) or None,
            options=domain_options or None,
        )

        # -- connection -----------------------------------------------------
        self.connection = connection
        if self.connection is None and domain and domain.connection_factory:
            self.connection = domain.connection_factory(self.config)

        # -- policy ---------------------------------------------------------
        if policy is not None:
            self.policy = policy
        elif domain and domain.policy_factory:
            self.policy = domain.policy_factory(self.config)
        else:
            raise ValueError("an Agent needs either a policy= or a domain with a policy_factory")

        # -- capabilities ----------------------------------------------------
        self.capabilities = {"skills"}
        if self.config.write:
            self.capabilities.add("write")
        if domain and domain.capability_factory:
            self.capabilities |= domain.capability_factory(self.config, self.connection)

        # -- skills ----------------------------------------------------------
        # Project skills are searched from the thing the agent is pointed at, not
        # from the process working directory. An agent given ./etl must pick up
        # ./etl/.agentlib/skills, whatever directory the caller happens to be in.
        self.skills: dict[str, Skill] = resolve_skills(
            bundled_dir=domain.skills_dir if domain else None,
            domain=domain.name if domain else None,
            extra_dirs=self.config.skill_dirs,
            disable=self.config.disable_skills,
            allow_plugins=self.config.allow_plugins,
            capabilities=self.capabilities,
            cwd=getattr(self.connection, "root", None),
        )

        # -- governance ------------------------------------------------------
        # Always on. Where it goes is the caller's choice; whether it exists
        # is not, because an unaudited agent cannot be reviewed.
        self.audit = audit or AuditLog(path=audit_path or self.config.option("audit_path"))

        # -- context ---------------------------------------------------------
        self.context = context_class(
            policy=self.policy,
            skills=self.skills,
            confirm_fn=confirm or deny_all,
            connection=self.connection,
            config=self.config,
            capabilities=self.capabilities,
            audit=self.audit,
        )

        # -- tools -----------------------------------------------------------
        self.tools = ToolRegistry()
        self.tools.extend(_core_tools())
        if domain:
            self.tools.extend(domain.load_tools())

        # -- MCP -------------------------------------------------------------
        # Third-party tools, namespaced so they cannot shadow a built-in, and
        # policed by their own operator-assigned tier.
        self.mcp: Any = None
        servers = mcp_servers if mcp_servers is not None else self.config.option("mcp_servers")
        if servers:
            from .mcp import MCPClient, servers_from_config

            resolved = servers_from_config(servers)
            self.mcp = MCPClient(resolved, write=self.config.write)
            self.capabilities.add("mcp")
            self.tools.extend(self.mcp.load_tools())
            self.audit.record(
                "mcp_configured",
                detail=", ".join(f"{s.name}({s.tier})" for s in resolved),
                outcome="connected",
            )

        self.tools.extend(tools)
        self.tools.filter_by(self.capabilities)

        # -- model -----------------------------------------------------------
        # One spec string selects the provider: "claude-opus-5",
        # "bedrock:amazon.nova-pro-v1:0", "openai:gpt-4o". A constructed Model
        # passes straight through.
        self.model: Model = resolve_model(
            model if model is not None else self.config.model,
            api_key=api_key,
            max_tokens=self.config.max_tokens,
            effort=self.config.effort,
            region=self.config.option("aws_region"),
            profile=self.config.option("aws_profile"),
            base_url=self.config.option("base_url"),
        )

        self.system_prompt_extra = system_prompt_extra
        # Optional narrowing of the advertised skill index. Selection happens
        # once per run() so the cached prompt prefix stays stable within a run.
        self.skill_selector = skill_selector
        self._advertised: dict[str, Skill] = dict(self.skills)
        self._messages: list[dict[str, Any]] = []
        self.usage = Usage()

        self.audit.set_manifest(self._manifest())

    def _manifest(self) -> RunManifest:
        """What this session was permitted to do, captured before it does anything."""
        import hashlib

        return RunManifest(
            domain=self.domain.name if self.domain else "",
            profile=self.config.profile,
            model=getattr(self.model, "model_id", type(self.model).__name__),
            policy=self.policy.describe(),
            write_enabled=self.config.write,
            connection=str(self.connection or ""),
            tools=self.tools.names(),
            destructive_tools=sorted(s.name for s in self.tools if s.destructive),
            skills=summarise_skills(self.skills.values()),
            mcp_servers=[s.name for s in getattr(self.mcp, "servers", [])] if self.mcp else [],
            plugins_allowed=self.config.allow_plugins,
            system_prompt_sha256=hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest(),
        )

    # -- introspection ------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        """The exact system prompt sent to the model. Inspect it; it is not magic."""
        return build_system_prompt(
            self._advertised.values(),
            domain_prompt=self._domain_prompt(),
            environment=self._environment(),
            write=self.policy.write,
            extra=self._extra_rules(),
            index_note=describe_selection(self._advertised, self.skills),
        )

    def _extra_rules(self) -> str:
        """The untrusted-content rule, plus whatever the caller added."""
        parts = [SYSTEM_RULE]
        if self.system_prompt_extra:
            parts.append(self.system_prompt_extra)
        return "\n\n".join(parts)

    def _domain_prompt(self) -> str:
        if not self.domain:
            return ""
        from importlib import import_module

        module = import_module(self.domain.package)
        return str(getattr(module, "SYSTEM_PROMPT", ""))

    def _environment(self) -> list[str]:
        lines: list[str] = []
        if self.domain:
            lines.append(f"Domain: **{self.domain.name}** -- {self.domain.description}")
        describe = getattr(self.connection, "describe_environment", None)
        if callable(describe):
            lines.extend(describe())
        elif self.connection is not None:
            lines.append(f"Connected to: {self.connection}")
        lines.append(f"Permissions: {self.policy.describe()}.")
        return lines

    def describe(self) -> str:
        """A human-readable summary of the resolved session. Use this to debug."""
        lines = [
            f"domain:    {self.domain.name if self.domain else '(none)'}",
            f"profile:   {self.config.profile}",
            f"model:     {getattr(self.model, 'model_id', type(self.model).__name__)}",
            f"policy:    {self.policy.describe()}",
            f"connected: {self.connection or '(nothing)'}",
            "",
            f"tools ({len(self.tools)}): {', '.join(self.tools.names())}",
            "",
            f"skills ({len(self.skills)}):",
        ]
        width = max((len(s) for s in self.skills), default=0)
        for skill in self.skills.values():
            lines.append(f"  - {skill.name:<{width}}  [{skill.source}]  {skill.origin}")
        return "\n".join(lines)

    @property
    def actions(self) -> list[ActionRecord]:
        """Every action this agent performed, in order."""
        return list(self.context.actions)

    @property
    def skills_used(self) -> list[str]:
        """Which skills the model actually loaded. The 'why did it do that' log."""
        return list(self.context.skills_used)

    @property
    def injection_attempts(self) -> list[Any]:
        """Tool results that carried apparent prompt-injection payloads."""
        return self.audit.injection_attempts

    @property
    def refusals(self) -> list[Any]:
        """Everything the policy layer blocked this session."""
        return self.audit.refusals

    def governance_report(self) -> str:
        """Manifest plus a summary of what happened. For the operator, after a run."""
        return self.audit.report()

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The raw transcript, for logging or replay."""
        return list(self._messages)

    # -- the loop -----------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[ToolCall], None] | None = None,
    ) -> RunResult:
        """Run one task to completion and return the final answer.

        The conversation persists on the agent, so consecutive calls continue the
        same session. Call :meth:`reset` to start over.
        """
        limit = max_turns or self.config.max_turns
        self._select_skills(prompt)
        self._messages.append(self.model.user_message(prompt))

        run_usage = Usage()
        all_calls: list[ToolCall] = []
        tool_defs = self.tools.to_anthropic()

        for turn in range(1, limit + 1):
            result = self.model.generate(
                system=self.system_prompt,
                messages=self._messages,
                tools=tool_defs,
                on_text=on_text,
            )
            self._messages.append(result.assistant_message)
            run_usage = run_usage + result.usage
            self.usage = self.usage + result.usage

            if not result.tool_calls:
                return RunResult(
                    text=result.text,
                    turns=turn,
                    usage=run_usage,
                    tool_calls=all_calls,
                    stop_reason=result.stop_reason,
                )

            all_calls.extend(result.tool_calls)
            results = []
            for call in result.tool_calls:
                if on_tool is not None:
                    on_tool(call)
                results.append(self._dispatch(call))
            self._messages.append(self.model.tool_result_message(results))

        raise MaxTurnsExceeded(
            f"stopped after {limit} turns without a final answer. Raise max_turns, or "
            "break the task into smaller steps."
        )

    def _select_skills(self, prompt: str) -> None:
        """Narrow the advertised index for this run, if a selector was given.

        Anything left out stays loadable by name -- the prompt says so -- so a
        retrieval miss costs one tool call rather than a lost capability.
        """
        if self.skill_selector is None:
            self._advertised = dict(self.skills)
            return
        try:
            chosen = self.skill_selector.select(prompt, self.skills)
        except Exception as exc:  # noqa: BLE001 - selection must not end a run
            logger.warning("skill selection failed, advertising everything: %s", exc)
            self._advertised = dict(self.skills)
            return

        # Whatever the model has already loaded stays advertised, so a follow-up
        # turn does not lose guidance it is mid-way through applying.
        for name in self.context.skills_used:
            if name in self.skills:
                chosen.setdefault(name, self.skills[name])

        self._advertised = {n: s for n, s in self.skills.items() if n in chosen}
        if len(self._advertised) != len(self.skills):
            self.audit.record(
                "skills_selected",
                detail=", ".join(self._advertised),
                outcome=f"{len(self._advertised)}/{len(self.skills)}",
            )

    @property
    def advertised_skills(self) -> dict[str, Skill]:
        """The skills currently in the system prompt. All of them unless narrowed."""
        return dict(self._advertised)

    def _dispatch(self, call: ToolCall) -> ToolResult:
        """Execute one tool call, converting failures into error results.

        Two things happen here besides calling the function:

        * every outcome is audited, refusals included -- that record is the
          governance artefact, and it is written whether or not anyone reads it;
        * output from any tool not marked ``trusted_output`` is sanitised and
          fenced before the model sees it, so file contents and query results
          arrive as data rather than as text in an instruction position.
        """
        spec: ToolSpec | None = self.tools.get(call.name)
        if spec is None:
            self.audit.record(
                "tool_call", tool=call.name, outcome="unknown_tool", detail=str(call.input)
            )
            return ToolResult(
                call_id=call.id,
                content=f"no such tool {call.name!r}. Available: {', '.join(self.tools.names())}",
                is_error=True,
            )

        self.audit.record(
            "tool_call",
            tool=call.name,
            detail=str(call.input),
            outcome="started",
            tier="destructive" if spec.destructive else "",
        )

        try:
            output = str(spec.fn(self.context, **call.input))
        except (ToolError, ConfirmationDenied) as exc:
            logger.info("tool %s refused or failed: %s", call.name, exc)
            self.audit.record("tool_call", tool=call.name, outcome="refused", detail=str(exc))
            return ToolResult(call_id=call.id, content=str(exc), is_error=True)
        except TypeError as exc:
            self.audit.record("tool_call", tool=call.name, outcome="bad_arguments", detail=str(exc))
            return ToolResult(
                call_id=call.id, content=f"invalid arguments for {call.name}: {exc}", is_error=True
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected failure is still a result
            logger.exception("tool %s raised", call.name)
            self.audit.record(
                "tool_call", tool=call.name, outcome="error", detail=f"{type(exc).__name__}: {exc}"
            )
            return ToolResult(
                call_id=call.id, content=f"{type(exc).__name__}: {exc}", is_error=True
            )

        if spec.trusted_output:
            self.audit.record(
                "tool_call", tool=call.name, outcome="ok", detail=f"{len(output)} chars"
            )
            return ToolResult(call_id=call.id, content=output)

        fenced, report = wrap_tool_result(output, nonce=self.context.nonce, source=call.name)
        if report.suspicious:
            logger.warning(
                "possible prompt injection in %s output: %s", call.name, report.summary()
            )
            self.audit.record(
                "injection_flagged",
                tool=call.name,
                source=call.name,
                outcome="flagged",
                detail="; ".join(str(f) for f in report.findings),
                categories=sorted({f.category for f in report.findings}),
            )
        self.audit.record(
            "tool_call",
            tool=call.name,
            outcome="ok",
            detail=f"{len(output)} chars",
            suspicious=report.suspicious,
        )
        return ToolResult(call_id=call.id, content=fenced)

    # -- session management -------------------------------------------------

    def reset(self) -> None:
        """Clear the conversation. Skills, tools and connections are kept."""
        self._messages.clear()

    def close(self) -> None:
        if self.mcp is not None:
            self.mcp.close()
        closer = getattr(self.connection, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> Agent:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        mode = "rw" if self.policy.write else "ro"
        name = self.domain.name if self.domain else "custom"
        target = self.connection or "no-connection"
        return f"<Agent {name} {target} {mode} skills={len(self.skills)} tools={len(self.tools)}>"


def _core_tools() -> list[ToolSpec]:
    """Tools every domain gets: the skill-loading three."""
    from . import skill_tools
    from .tools import collect

    return collect(skill_tools)


__all__ = [
    "ActionRecord",
    "Agent",
    "AgentContext",
    "ConfirmFn",
    "confirm_in_terminal",
    "deny_all",
]
