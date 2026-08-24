"""Skill discovery, parsing and precedence resolution.

A *skill* is a directory containing a ``SKILL.md`` file with YAML frontmatter::

    skills/incremental-backfill/
    |-- SKILL.md
    `-- reference/reconciliation.md

Only the ``name`` and ``description`` of each resolved skill reach the system
prompt. The body is loaded on demand through the ``load_skill`` tool --
progressive disclosure, so a large library costs a few hundred tokens rather
than a few hundred thousand.

Resolution order, later winning on a name collision::

    1. bundled          the domain's own skills/ directory
    2. plugin           installed skill packs -- opt-in, see ``allow_plugins``
    3. user shared      ~/.agentlib/skills/
    4. user domain      ~/.agentlib/skills/<domain>/
    5. project shared   ./.agentlib/skills/
    6. project domain   ./.agentlib/skills/<domain>/
    7. explicit         directories passed to the agent

Shared tiers apply to every domain, which is where house style belongs; domain
tiers apply to one. A team overrides a bundled skill by committing a same-named
directory -- no fork, no monkeypatching.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import yaml

from .errors import SkillError

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
ENTRY_POINT_GROUP = "agentsuite.skills"

#: Sources in ascending precedence order.
SOURCE_ORDER = (
    "bundled",
    "plugin",
    "user",
    "user:domain",
    "project",
    "project:domain",
    "explicit",
)

CONFIG_DIRNAME = ".agentlib"
USER_DIR = Path.home() / CONFIG_DIRNAME
PROJECT_SKILLS_SUBDIR = f"{CONFIG_DIRNAME}/skills"

_BOM = "﻿"


@dataclass(frozen=True)
class Skill:
    """A parsed skill file plus the provenance needed to debug its behaviour."""

    name: str
    description: str
    body: str
    path: Path
    source: str
    origin: str = ""
    requires: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.path.parent

    def index_entry(self) -> str:
        """The one-line form that goes into the system prompt."""
        return f"- **{self.name}**: {self.description}"

    def list_files(self) -> list[str]:
        """Relative paths of supporting files bundled alongside ``SKILL.md``."""
        out: list[str] = []
        for candidate in sorted(self.directory.rglob("*")):
            if candidate.is_file() and candidate.name != SKILL_FILE:
                out.append(candidate.relative_to(self.directory).as_posix())
        return out

    def read_file(self, relpath: str) -> str:
        """Read a supporting file, refusing to escape the skill directory."""
        base = self.directory.resolve()
        target = (base / relpath).resolve()
        if not _is_relative_to(target, base):
            raise SkillError(f"{relpath!r} escapes the skill directory for {self.name!r}")
        if not target.is_file():
            raise SkillError(f"{self.name!r} has no file {relpath!r}")
        return target.read_text(encoding="utf-8")


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def parse_skill_file(path: Path, *, source: str, origin: str = "") -> Skill:
    """Parse a single ``SKILL.md``.

    Raises :class:`SkillError` naming the offending path -- a malformed skill in
    someone's project directory should be obvious, not a silent no-op.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem edge case
        raise SkillError(f"cannot read {path}: {exc}") from exc

    front, body = _split_frontmatter(raw, path)

    name = front.get("name") or path.parent.name
    if not isinstance(name, str) or not name.strip():
        raise SkillError(f"{path}: 'name' must be a non-empty string")

    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            f"{path}: 'description' is required and must be a non-empty string. "
            "It is the only text the model sees before deciding to load the skill."
        )

    requires = front.get("requires") or []
    if isinstance(requires, str):
        requires = [requires]
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        raise SkillError(f"{path}: 'requires' must be a string or list of strings")

    known = {"name", "description", "requires"}
    metadata = {k: v for k, v in front.items() if k not in known}

    return Skill(
        name=name.strip(),
        description=" ".join(description.split()),
        body=body.strip(),
        path=path,
        source=source,
        origin=origin or str(path.parent),
        requires=tuple(requires),
        metadata=metadata,
    )


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    text = raw.lstrip(_BOM)
    if not text.startswith("---"):
        raise SkillError(
            f"{path}: missing YAML frontmatter. A skill file must start with a '---' block "
            "declaring at least 'name' and 'description'."
        )
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillError(f"{path}: frontmatter block is not terminated by a closing '---'")
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(front, dict):
        raise SkillError(f"{path}: frontmatter must be a YAML mapping")
    return front, parts[2]


def discover_dir(directory: Path, *, source: str, origin: str = "") -> list[Skill]:
    """Parse every skill directly beneath ``directory``.

    A subdirectory without ``SKILL.md`` is skipped silently. That is what lets a
    shared skills directory hold per-domain subdirectories alongside skills.
    """
    if not directory.is_dir():
        return []
    skills: list[Skill] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        skill_file = child / SKILL_FILE
        if skill_file.is_file():
            skills.append(
                parse_skill_file(skill_file, source=source, origin=origin or str(directory))
            )
    return skills


def discover_plugins() -> list[Skill]:
    """Load skills from installed packages advertising the entry point."""
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001 - metadata reads can fail on odd installs
        logger.warning("could not enumerate skill packs: %s", exc)
        return []

    skills: list[Skill] = []
    for ep in eps:
        try:
            target = ep.load()
        except Exception as exc:  # noqa: BLE001 - a broken pack must not kill the agent
            logger.warning("skill pack %r failed to load: %s", ep.name, exc)
            continue

        if isinstance(target, (str, Path)):
            directory = Path(target)
        else:
            module_file = getattr(target, "__file__", None)
            if not module_file:
                logger.warning("skill pack %r resolved to something without a path", ep.name)
                continue
            directory = Path(module_file).parent

        if directory.name != "skills" and (directory / "skills").is_dir():
            directory = directory / "skills"

        try:
            skills.extend(discover_dir(directory, source="plugin", origin=f"plugin:{ep.name}"))
        except SkillError as exc:
            logger.warning("skill pack %r contains an invalid skill: %s", ep.name, exc)
    return skills


def find_project_skills_dir(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``.agentlib/skills``.

    Stops before the home directory. Without that guard, running from anywhere
    under ``~`` finds the *user* directory and relabels it as a project one --
    the same skills counted twice, under the wrong precedence.
    """
    current = (start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - no home directory
        home = None

    for directory in (current, *current.parents):
        if home is not None and directory == home:
            break
        candidate = directory / PROJECT_SKILLS_SUBDIR
        if candidate.is_dir():
            return candidate
    return None


def resolve_skills(
    *,
    bundled_dir: Path | None = None,
    domain: str | None = None,
    extra_dirs: Sequence[str | Path] = (),
    disable: Iterable[str] = (),
    allow_plugins: bool = False,
    capabilities: Iterable[str] | None = None,
    user_dir: Path | None = None,
    project_dir: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Skill]:
    """Resolve the effective skill set for a session.

    ``capabilities`` filters on each skill's ``requires:`` list -- there is no
    point advertising dbt skills to a session with no dbt project. Pass ``None``
    to disable filtering.
    """
    user_root = (
        user_dir if user_dir is not None else _env_dir("AGENT_USER_SKILLS", USER_DIR / "skills")
    )
    project_root = project_dir if project_dir is not None else find_project_skills_dir(cwd)

    layers: list[list[Skill]] = [
        discover_dir(bundled_dir, source="bundled", origin="bundled") if bundled_dir else [],
        discover_plugins() if allow_plugins else [],
        discover_dir(user_root, source="user", origin=str(user_root)),
    ]

    if domain:
        layers.append(
            discover_dir(user_root / domain, source="user:domain", origin=str(user_root / domain))
        )

    if project_root:
        layers.append(discover_dir(project_root, source="project", origin=str(project_root)))
        if domain:
            layers.append(
                discover_dir(
                    project_root / domain,
                    source="project:domain",
                    origin=str(project_root / domain),
                )
            )

    explicit: list[Skill] = []
    for directory in extra_dirs:
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise SkillError(f"skill directory does not exist: {path}")
        # Accept either a directory *of* skills, or a single skill directory.
        if (path / SKILL_FILE).is_file():
            explicit.append(
                parse_skill_file(path / SKILL_FILE, source="explicit", origin=str(path))
            )
        else:
            explicit.extend(discover_dir(path, source="explicit", origin=str(path)))
    layers.append(explicit)

    merged: dict[str, Skill] = {}
    for layer in layers:
        for skill in layer:
            merged[skill.name] = skill

    for name in disable:
        merged.pop(name, None)

    if capabilities is not None:
        available = set(capabilities)
        merged = {
            name: skill
            for name, skill in merged.items()
            if not skill.requires or available.issuperset(skill.requires)
        }

    return dict(sorted(merged.items()))


def _env_dir(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


def render_index(skills: Iterable[Skill]) -> str:
    """Render the skill index injected into the system prompt."""
    entries = [s.index_entry() for s in skills]
    if not entries:
        return "(No skills are loaded for this session.)"
    return "\n".join(entries)


__all__ = [
    "CONFIG_DIRNAME",
    "ENTRY_POINT_GROUP",
    "SOURCE_ORDER",
    "USER_DIR",
    "Skill",
    "discover_dir",
    "discover_plugins",
    "find_project_skills_dir",
    "parse_skill_file",
    "render_index",
    "resolve_skills",
]
