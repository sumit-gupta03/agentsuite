"""Optional retrieval over the skill library.

Progressive disclosure already keeps the prompt small: only each skill's name and
description reach it, so twenty skills cost a few hundred tokens. At two hundred
skills that index is several thousand tokens on **every turn**, and paying for
guidance that will not be used starts to matter.

A selector narrows the advertised index to the skills a request plausibly needs.

::

    import agentsuite as agent

    dev = agent.pyspark(project="./etl", skill_selector=agent.KeywordSelector(limit=8))

    # Or bring your own vector store -- pgvector, Chroma, whatever you run:
    dev = agent.pyspark(
        project="./etl",
        skill_selector=agent.EmbeddingSelector(embed=my_embedder, limit=8),
    )

Three properties this design keeps
----------------------------------

**Selection happens once per** :meth:`~agentsuite.core.loop.Agent.run`, not per turn.
The system prompt is the cached prefix of every turn in a run; recomputing it
mid-run would invalidate that cache and cost more than the index ever saved.

**Nothing becomes unreachable.** A skill left out of the index can still be
fetched by name with ``load_skill``, and the prompt says so. A retrieval miss
costs one tool call, never a lost capability.

**Off by default.** Below roughly fifty skills the full index is cheaper than an
embedding round trip, and simpler. Turn this on when the index is genuinely
large.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .skills import Skill

logger = logging.getLogger(__name__)

#: Below this many skills, retrieval is not worth its complexity or latency.
WORTHWHILE_ABOVE = 25

_WORD = re.compile(r"[a-z0-9_]+")

#: Words too common in this domain to discriminate between skills.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can could do does for from has have how i if in
    into is it its me my no not of on or our should so than that the their then
    there these they this to use used using was we what when where which who why
    will with would you your use covers when
    """.split()
)


@runtime_checkable
class SkillSelector(Protocol):
    """Chooses which skills to advertise for a given request."""

    def select(self, prompt: str, skills: dict[str, Skill]) -> dict[str, Skill]:
        """Return the subset to put in the system prompt, in a stable order."""
        ...


def tokenise(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2]


@dataclass
class KeywordSelector:
    """Lexical selection with no dependencies and no network call.

    Scores each skill's name and description against the request using BM25-style
    term weighting: a word that appears in one skill discriminates, a word in
    every skill does not.

    Good enough for most libraries, and it costs nothing. Reach for
    :class:`EmbeddingSelector` when requests and skill descriptions use different
    vocabulary for the same thing.
    """

    limit: int = 10
    #: Skills always advertised regardless of score -- house style, conventions.
    always: tuple[str, ...] = ()
    #: Below this score a skill is not advertised at all.
    floor: float = 0.0

    def select(self, prompt: str, skills: dict[str, Skill]) -> dict[str, Skill]:
        if len(skills) <= self.limit:
            return skills

        scores = self.score(prompt, skills)
        chosen = {name for name in self.always if name in skills}
        for name, score in scores:
            if len(chosen) >= self.limit:
                break
            if score > self.floor:
                chosen.add(name)

        # Preserve the library's own ordering, so the rendered index is stable
        # for any given selection and the prompt cache can still hit.
        return {name: skill for name, skill in skills.items() if name in chosen}

    def score(self, prompt: str, skills: dict[str, Skill]) -> list[tuple[str, float]]:
        """``(name, score)`` for every skill, best first. Exposed for debugging."""
        terms = tokenise(prompt)
        if not terms:
            return [(name, 0.0) for name in skills]

        documents = {
            name: tokenise(f"{skill.name} {skill.name.replace('-', ' ')} {skill.description}")
            for name, skill in skills.items()
        }
        total = len(documents) or 1
        frequency: dict[str, int] = {}
        for words in documents.values():
            for word in set(words):
                frequency[word] = frequency.get(word, 0) + 1

        ranked: list[tuple[str, float]] = []
        for name, words in documents.items():
            counts: dict[str, int] = {}
            for word in words:
                counts[word] = counts.get(word, 0) + 1
            score = 0.0
            for term in terms:
                if term not in counts:
                    continue
                # Inverse document frequency: a term in every skill tells us nothing.
                idf = math.log(1 + total / (1 + frequency.get(term, 0)))
                score += idf * (1 + math.log(counts[term]))
            ranked.append((name, score))

        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked

    def __repr__(self) -> str:
        return f"<KeywordSelector limit={self.limit}>"


@dataclass
class EmbeddingSelector:
    """Semantic selection using whatever embedder and store you already run.

    Args:
        embed: ``list[str] -> list[list[float]]``. Anything: an API embedder, a
            local sentence-transformer, a database function.
        store: Optional external vector store. Implement :class:`VectorStore` to
            use pgvector, Chroma, Qdrant or similar. Without one, embeddings are
            computed once per session and kept in memory, which is usually enough
            -- a skill library is hundreds of rows, not millions.
        limit: How many skills to advertise.
        always: Skills advertised regardless of score.

    Embeddings for the library are computed **once**, on first use, not per turn.
    """

    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]]
    store: VectorStore | None = None
    limit: int = 10
    always: tuple[str, ...] = ()
    #: Falls back to lexical selection if embedding fails, rather than failing.
    fallback: SkillSelector | None = None

    _vectors: dict[str, Sequence[float]] = field(default_factory=dict, init=False, repr=False)
    _indexed: frozenset[str] = field(default_factory=frozenset, init=False, repr=False)

    def select(self, prompt: str, skills: dict[str, Skill]) -> dict[str, Skill]:
        if len(skills) <= self.limit:
            return skills
        try:
            ranked = self._rank(prompt, skills)
        except Exception as exc:  # noqa: BLE001 - retrieval must never end a run
            logger.warning("skill embedding failed, falling back to lexical: %s", exc)
            selector = self.fallback or KeywordSelector(limit=self.limit, always=self.always)
            return selector.select(prompt, skills)

        chosen = {name for name in self.always if name in skills}
        for name, _ in ranked:
            if len(chosen) >= self.limit:
                break
            chosen.add(name)
        return {name: skill for name, skill in skills.items() if name in chosen}

    def _rank(self, prompt: str, skills: dict[str, Skill]) -> list[tuple[str, float]]:
        self._ensure_indexed(skills)
        query = list(self.embed([prompt])[0])

        if self.store is not None:
            found = self.store.search(query, limit=max(self.limit * 2, self.limit))
            return [(name, score) for name, score in found if name in skills]

        ranked = [
            (name, _cosine(query, vector))
            for name, vector in self._vectors.items()
            if name in skills
        ]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked

    def _ensure_indexed(self, skills: dict[str, Skill]) -> None:
        """Embed anything new. Skills rarely change, so this runs once."""
        current = frozenset(skills)
        missing = [name for name in skills if name not in self._vectors]
        if not missing and current == self._indexed:
            return

        texts = [_document(skills[name]) for name in missing]
        if texts:
            vectors = self.embed(texts)
            pairs = list(zip(missing, vectors, strict=False))
            for name, vector in pairs:
                self._vectors[name] = list(vector)
            if self.store is not None:
                self.store.upsert([(name, list(vector), _document(skills[name]))
                                   for name, vector in pairs])
        self._indexed = current

    def __repr__(self) -> str:
        backing = type(self.store).__name__ if self.store else "in-memory"
        return f"<EmbeddingSelector limit={self.limit} store={backing}>"


@runtime_checkable
class VectorStore(Protocol):
    """What this library needs from a vector database.

    Deliberately two methods. Implement it over pgvector, Chroma, Qdrant, or a
    table you already have -- nothing here assumes a particular product, and no
    vector database is a dependency of this package.
    """

    def upsert(self, records: Sequence[tuple[str, Sequence[float], str]]) -> None:
        """Store ``(skill_name, vector, text)`` triples."""
        ...

    def search(self, vector: Sequence[float], *, limit: int) -> Sequence[tuple[str, float]]:
        """Return ``(skill_name, score)``, most similar first."""
        ...


def _document(skill: Skill) -> str:
    """What gets embedded: the name and the description, not the body.

    The description already states *when to use this*, which is exactly the
    matching signal. Embedding the body would dilute it with implementation
    detail that has nothing to do with whether the skill applies.
    """
    return f"{skill.name.replace('-', ' ')}. {skill.description}"


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def describe_selection(
    selected: Iterable[str], available: dict[str, Skill]
) -> str:
    """The note appended to a narrowed index, so nothing looks unreachable."""
    chosen = set(selected)
    hidden = [name for name in available if name not in chosen]
    if not hidden:
        return ""
    listed = ", ".join(hidden[:40])
    more = f", and {len(hidden) - 40} more" if len(hidden) > 40 else ""
    return (
        f"\n\n_{len(hidden)} further skill(s) are available but not listed above: "
        f"{listed}{more}. Load any of them by name with `load_skill` if the task "
        "calls for it._"
    )


def worth_retrieving(skills: dict[str, Any]) -> bool:
    """Whether a library is large enough for retrieval to pay for itself."""
    return len(skills) > WORTHWHILE_ABOVE


__all__ = [
    "WORTHWHILE_ABOVE",
    "EmbeddingSelector",
    "KeywordSelector",
    "SkillSelector",
    "VectorStore",
    "describe_selection",
    "tokenise",
    "worth_retrieving",
]
