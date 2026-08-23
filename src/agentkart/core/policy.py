"""The permission layer, shared by every domain.

A domain decides *what an action is* -- parsing SQL, resolving a path, reading an
argv list. This module decides *whether it may happen*, and that decision is
identical everywhere:

* three tiers, ``read`` / ``write`` / ``destructive``
* anything unclassifiable is destructive, never read -- fail closed
* writes require an explicitly write-enabled session
* destructive actions additionally require an affirmative confirmation
* a refusal is returned, not raised, so the model can choose something safer

Subclass :class:`Policy` and implement :meth:`Policy.classify`. Everything above
comes for free, and no domain gets to reinvent it -- which is the point, because
a permission layer reimplemented per domain is a permission layer with three
different bugs in it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["read", "write", "destructive"]

#: Ascending severity. The highest tier present in a request governs it.
TIER_RANK: dict[Tier, int] = {"read": 0, "write": 1, "destructive": 2}


@dataclass(frozen=True)
class Action:
    """One thing a tool proposes to do, classified but not yet permitted."""

    #: What sort of action -- "sql", "file_write", "subprocess", ...
    kind: str
    #: The thing itself: a statement, a path, a rendered command.
    detail: str
    tier: Tier
    #: Short label for the refusal message, e.g. "DROP" or "write outside root".
    label: str = ""
    #: Why this tier, when it is not obvious. Shown to the model on refusal.
    reason: str = ""

    def describe(self) -> str:
        name = self.label or self.kind.upper()
        return f"{name} ({self.reason})" if self.reason else name


@dataclass(frozen=True)
class Verdict:
    """The outcome of checking a request against a policy."""

    allowed: bool
    tier: Tier
    actions: list[Action] = field(default_factory=list)
    needs_confirmation: bool = False
    reason: str = ""
    #: A safer form of the request, when the policy could produce one (for
    #: instance a SELECT with a LIMIT added). ``None`` means run it as given.
    rewritten: str | None = None

    @property
    def primary(self) -> Action | None:
        """The action that determined the verdict."""
        for action in self.actions:
            if action.tier == self.tier:
                return action
        return self.actions[0] if self.actions else None


@dataclass(frozen=True)
class Policy(ABC):
    """Base permission policy. Domains subclass and implement ``classify``."""

    #: Permit actions that change state.
    write: bool = False
    #: Permit destructive actions without asking. Set only after confirmation.
    allow_destructive: bool = False

    # -- the part each domain supplies -------------------------------------

    @abstractmethod
    def classify(self, request: str, **context: Any) -> list[Action]:
        """Turn a raw request into classified actions.

        Must fail closed: anything the domain cannot confidently recognise is
        ``destructive``. Returning ``read`` for an unparsed input is the one
        mistake this design cannot tolerate.
        """

    # -- optional hooks ----------------------------------------------------

    def validate(self, actions: list[Action]) -> str | None:
        """Reject a set of actions for structural reasons.

        Return a refusal message, or ``None`` to continue. Used for rules that
        are about the *batch* rather than any single action -- for example
        refusing several statements submitted in one call.
        """
        return None

    def rewrite(self, request: str, actions: list[Action], **context: Any) -> str | None:
        """Return a safer equivalent of ``request``, or ``None`` to leave it be."""
        return None

    def describe(self) -> str:
        """One line summarising what this policy permits."""
        if not self.write:
            return "read-only"
        return "read-write, destructive actions require confirmation"

    # -- the shared decision -----------------------------------------------

    def check(self, request: str, **context: Any) -> Verdict:
        """Decide whether ``request`` may proceed."""
        actions = self.classify(request, **context)

        if not actions:
            return Verdict(False, "read", [], reason="refused: nothing to do")

        structural = self.validate(actions)
        if structural is not None:
            return Verdict(False, "read", actions, reason=structural)

        tier: Tier = max((a.tier for a in actions), key=lambda t: TIER_RANK[t])
        offending = next((a for a in actions if a.tier == tier), actions[0])

        if tier != "read" and not self.write:
            return Verdict(
                False,
                tier,
                actions,
                reason=(
                    f"refused: this session is read-only and the action is "
                    f"{offending.describe()}. Construct the agent with write=True to allow it."
                ),
            )

        if tier == "destructive" and not self.allow_destructive:
            return Verdict(
                False,
                tier,
                actions,
                needs_confirmation=True,
                reason=(
                    f"refused: {offending.describe()} is destructive. "
                    "It requires explicit confirmation."
                ),
            )

        return Verdict(
            True,
            tier,
            actions,
            needs_confirmation=tier == "destructive",
            rewritten=self.rewrite(request, actions, **context) if tier == "read" else None,
        )


__all__ = ["TIER_RANK", "Action", "Policy", "Tier", "Verdict"]
