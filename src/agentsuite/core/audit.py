"""Governance: an evidential record of what an agent did and why.

Three things a reviewer asks after the fact, and where each is answered:

*What was this agent allowed to do?*     :class:`RunManifest`
*What did it actually do?*               :class:`AuditLog` events
*Why did it do that?*                    the skills it loaded, recorded per run

The log is append-only JSON Lines, one event per line, with secrets redacted on
the way in rather than on the way out. It is designed to be shipped somewhere
durable and read by someone who was not present.

Nothing here is optional-but-recommended: the loop writes to a log always. Where
that log goes -- memory, a file, your own sink -- is the caller's choice.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Redaction happens before anything is written. Patterns are deliberately broad:
#: a false positive costs a reviewer nothing, a false negative writes a live key
#: into a file someone will email around.
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)\b(sk-ant-[A-Za-z0-9_\-]{16,})", "anthropic-key"),
    (r"(?i)\b(sk-[A-Za-z0-9]{20,})", "api-key"),
    (r"\b(gh[pousr]_[A-Za-z0-9]{20,})", "github-token"),
    (r"\b(AKIA[0-9A-Z]{16})\b", "aws-access-key"),
    (r"\b(ASIA[0-9A-Z]{16})\b", "aws-temp-key"),
    (r"(?i)\b(xox[baprs]-[A-Za-z0-9\-]{10,})", "slack-token"),
    (r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", "jwt"),
    (r"(?i)(?:password|passwd|pwd|secret|token|api[_\-]?key)\s*[=:]\s*[\"']?([^\s\"',;)]{6,})",
     "assigned-secret"),
    (r"(?i)://[^\s:/@]+:([^\s@/]{3,})@", "dsn-password"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private-key"),
)

REDACTED = "[REDACTED:{label}]"


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a labelled marker."""
    if not text:
        return text
    for pattern, label in SECRET_PATTERNS:
        marker = REDACTED.format(label=label)

        def _sub(match: re.Match[str], marker: str = marker) -> str:
            # Replace the captured secret if there is one, else the whole match,
            # so surrounding context (a URL, a key header) survives for review.
            if match.groups():
                whole, secret = match.group(0), match.group(1)
                return whole.replace(secret, marker)
            return marker

        text = re.sub(pattern, _sub, text)
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit:,} more characters]"


@dataclass
class AuditEvent:
    """One recorded thing. ``seq`` orders events within a run."""

    seq: int
    kind: str
    detail: str = ""
    tier: str = ""
    outcome: str = ""
    tool: str = ""
    purpose: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class RunManifest:
    """What this session was permitted to do, captured before it does anything.

    Reviewed alongside the events: the events say what happened, the manifest
    says what *could* have happened.
    """

    domain: str = ""
    profile: str = ""
    model: str = ""
    policy: str = ""
    write_enabled: bool = False
    connection: str = ""
    tools: list[str] = field(default_factory=list)
    destructive_tools: list[str] = field(default_factory=list)
    skills: list[dict[str, str]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    plugins_allowed: bool = False
    system_prompt_sha256: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, indent=2)

    def summary(self) -> str:
        lines = [
            f"domain:      {self.domain or '(none)'}",
            f"model:       {self.model}",
            f"policy:      {self.policy}",
            f"connection:  {self.connection or '(none)'}",
            f"tools:       {len(self.tools)} ({len(self.destructive_tools)} destructive)",
            f"skills:      {len(self.skills)}",
            f"prompt hash: {self.system_prompt_sha256[:16]}",
        ]
        if self.mcp_servers:
            lines.append(f"mcp servers: {', '.join(self.mcp_servers)}")
        if self.plugins_allowed:
            lines.append("plugins:     ENABLED (third-party skills may be loaded)")
        return "\n".join(lines)


class AuditLog:
    """Append-only record of a session.

    Thread-safe, redacting, and cheap when nobody is watching -- the in-memory
    log keeps a bounded buffer, and a ``path`` streams every event to disk.
    """

    def __init__(
        self,
        *,
        path: str | os.PathLike[str] | None = None,
        sink: Callable[[AuditEvent], None] | None = None,
        max_events: int = 5_000,
        max_detail: int = 4_000,
        redact_secrets: bool = True,
    ) -> None:
        self.path = Path(path) if path else None
        self.sink = sink
        self.max_events = max_events
        self.max_detail = max_detail
        self.redact_secrets = redact_secrets
        self.manifest = RunManifest()
        self._events: list[AuditEvent] = []
        self._seq = 0
        self._lock = threading.Lock()

        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing ------------------------------------------------------------

    def record(
        self,
        kind: str,
        *,
        detail: str = "",
        tier: str = "",
        outcome: str = "",
        tool: str = "",
        purpose: str = "",
        source: str = "",
        **metadata: Any,
    ) -> AuditEvent:
        """Append an event. Returns it, so callers can reference the sequence."""
        clean = _truncate(detail or "", self.max_detail)
        if self.redact_secrets:
            clean = redact(clean)
            purpose = redact(purpose)

        with self._lock:
            self._seq += 1
            event = AuditEvent(
                seq=self._seq,
                kind=kind,
                detail=clean,
                tier=tier,
                outcome=outcome,
                tool=tool,
                purpose=purpose,
                source=source,
                metadata=metadata,
            )
            self._events.append(event)
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]

        self._emit(event)
        return event

    def _emit(self, event: AuditEvent) -> None:
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(event.to_json() + "\n")
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("could not write audit event to %s: %s", self.path, exc)
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop the run
                logger.warning("audit sink raised: %s", exc)

    def set_manifest(self, manifest: RunManifest) -> None:
        self.manifest = manifest
        self.record("manifest", detail=manifest.to_json(), outcome="recorded")

    # -- reading ------------------------------------------------------------

    @property
    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def of_kind(self, *kinds: str) -> list[AuditEvent]:
        wanted = set(kinds)
        return [e for e in self.events if e.kind in wanted]

    #: Outcomes that mean "the agent wanted to do this and was not allowed to".
    #: ``unknown_tool`` counts: a capability-gated tool is absent precisely
    #: because this session may not use it, and the attempt is worth surfacing.
    REFUSED_OUTCOMES = frozenset({"refused", "denied", "unknown_tool"})

    @property
    def refusals(self) -> list[AuditEvent]:
        """Everything the agent was not allowed to do. A reviewer reads this first."""
        return [e for e in self.events if e.outcome in self.REFUSED_OUTCOMES]

    @property
    def injection_attempts(self) -> list[AuditEvent]:
        """Tool results that carried apparent prompt-injection payloads."""
        return [e for e in self.events if e.kind == "injection_flagged"]

    def to_jsonl(self) -> str:
        return "\n".join(event.to_json() for event in self.events)

    def report(self) -> str:
        """A short human-readable summary, for the end of a run."""
        events = self.events
        actions = [e for e in events if e.kind == "action"]
        refusals = self.refusals
        injections = self.injection_attempts

        lines = [
            "AUDIT SUMMARY",
            "-" * 60,
            self.manifest.summary(),
            "-" * 60,
            f"events:    {len(events)}",
            f"actions:   {len(actions)}",
            f"refusals:  {len(refusals)}",
            f"injection: {len(injections)} flagged",
        ]
        if refusals:
            lines.append("\nRefused:")
            for event in refusals[:20]:
                lines.append(f"  [{event.seq}] {event.tool or event.kind}: {event.detail[:100]}")
        if injections:
            lines.append("\nApparent injection attempts:")
            for event in injections[:20]:
                lines.append(f"  [{event.seq}] via {event.source}: {event.detail[:100]}")
        return "\n".join(lines)


def summarise_skills(skills: Iterable[Any]) -> list[dict[str, str]]:
    """Render skills for a manifest: what guidance was available, and from where."""
    return [
        {"name": s.name, "source": s.source, "origin": str(s.origin)}
        for s in skills
    ]


__all__ = [
    "SECRET_PATTERNS",
    "AuditEvent",
    "AuditLog",
    "RunManifest",
    "redact",
    "summarise_skills",
]
