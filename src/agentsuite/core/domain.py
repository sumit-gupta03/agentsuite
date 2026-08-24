"""Domains and presets.

A **domain** is what makes an agent able to *do* something: a tool set, a
permission policy, a skill library, and optionally a connection (a warehouse, a
project directory, a cloud account). Domains are the expensive thing to add.

A **preset** is a domain plus configuration plus extra skills, and costs no code
at all. ``snowflake`` is the ``dataengineering`` domain pinned to one warehouse
with Snowflake-specific skills layered on; it is not a second agent.

Most of what people want is a preset. Reach for a new domain only when the tools
genuinely differ -- when there is something the existing tools cannot do.

Domains register through the ``agentsuite.domains`` entry point, so a third-party
package can add one without this library knowing it exists.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ConfigError

if TYPE_CHECKING:
    from .config import Config
    from .policy import Policy

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "agentsuite.domains"


@dataclass(frozen=True)
class Domain:
    """Everything that makes an agent domain-specific.

    Deliberately small. If adding a domain requires touching :mod:`agentsuite.core`,
    the abstraction is wrong and the core should grow a hook instead.
    """

    name: str
    description: str

    #: Import path of the package holding the domain, e.g.
    #: ``"agentsuite.domains.dataengineering"``. Its ``skills/`` directory is the
    #: bundled skill library.
    package: str

    #: Modules holding ``@tool``-decorated functions, relative to ``package``.
    tool_modules: tuple[str, ...] = ()

    #: Builds the permission policy from resolved config.
    policy_factory: Callable[[Config], Policy] | None = None

    #: Builds the domain's connection (warehouse, project, cloud session).
    #: ``None`` for domains that need no connection.
    connection_factory: Callable[[Config], Any] | None = None

    #: Extra capability names contributed by an established connection. These
    #: gate both tools and skills via their ``requires:`` lists.
    capability_factory: Callable[[Config, Any], set[str]] | None = None

    #: The pip extra providing this domain's dependencies, for error messages.
    extra: str = ""

    #: Alternative names accepted on the CLI and in ``agent.<name>``.
    aliases: tuple[str, ...] = ()

    #: Presets shipped with the domain: name -> kwargs merged into the factory.
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: preset name -> when to use it. Read by the router, and by `agent domains`.
    #: A preset with no description is still usable, just not routable.
    preset_descriptions: dict[str, str] = field(default_factory=dict)

    @property
    def skills_dir(self) -> Path:
        """The bundled skill library for this domain."""
        module = import_module(self.package)
        location = getattr(module, "__file__", None)
        if not location:  # pragma: no cover - namespace package
            raise ConfigError(f"domain {self.name!r} has no importable location")
        return Path(location).parent / "skills"

    def load_tools(self) -> list[Any]:
        """Import the domain's tool modules and return their tool specs."""
        from .tools import collect

        specs: list[Any] = []
        for name in self.tool_modules:
            module = import_module(f"{self.package}.{name}")
            specs.extend(collect(module))
        return specs

    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class DomainRegistry:
    """Resolves domain names to :class:`Domain` objects."""

    #: Domains shipped in this package: name -> "module:attribute".
    BUILTIN: dict[str, str] = {
        "dataengineering": "agentsuite.domains.dataengineering:DOMAIN",
        "code": "agentsuite.domains.code:DOMAIN",
    }

    def __init__(self) -> None:
        self._cache: dict[str, Domain] = {}
        self._scanned = False

    def register(self, domain: Domain) -> None:
        """Register a domain built at runtime."""
        for name in domain.all_names():
            self._cache[name] = domain

    def get(self, name: str) -> Domain:
        key = name.replace("-", "").replace("_", "").lower()

        if key in self._cache:
            return self._cache[key]

        target = self.BUILTIN.get(key)
        if target:
            domain = self._load(target)
            self.register(domain)
            return domain

        # An alias only lands in the cache once its domain has been loaded, so
        # a first-ever lookup of "de" must load the built-ins before giving up.
        self._load_builtins()
        if key in self._cache:
            return self._cache[key]

        self._scan_plugins()
        if key in self._cache:
            return self._cache[key]

        known = ", ".join(self.names())
        raise ConfigError(f"unknown domain {name!r}. Available: {known}")

    def names(self) -> list[str]:
        """Every domain name that can be resolved, aliases included."""
        self._scan_plugins()
        return sorted({*self.BUILTIN, *self._cache})

    def _load_builtins(self) -> None:
        for name, target in self.BUILTIN.items():
            if name in self._cache:
                continue
            try:
                self.register(self._load(target))
            except ConfigError as exc:  # pragma: no cover - broken install
                logger.warning("built-in domain %r failed to load: %s", name, exc)

    def describe(self) -> list[tuple[str, str]]:
        """``(name, description)`` for every resolvable domain. Imports them."""
        out: list[tuple[str, str]] = []
        for name in self.names():
            try:
                domain = self.get(name)
            except ConfigError as exc:  # pragma: no cover - broken install
                out.append((name, f"(unavailable: {exc})"))
                continue
            if domain.name == name:
                out.append((domain.name, domain.description))
        return out

    def _scan_plugins(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        try:
            found = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception as exc:  # noqa: BLE001 - odd installs must not be fatal
            logger.warning("could not enumerate domain plugins: %s", exc)
            return
        for ep in found:
            try:
                domain = ep.load()
            except Exception as exc:  # noqa: BLE001 - one bad plugin is not fatal
                logger.warning("domain plugin %r failed to load: %s", ep.name, exc)
                continue
            if isinstance(domain, Domain):
                self.register(domain)
            else:
                logger.warning("domain plugin %r did not provide a Domain", ep.name)

    @staticmethod
    def _load(target: str) -> Domain:
        module_path, _, attribute = target.partition(":")
        try:
            module = import_module(module_path)
        except ImportError as exc:
            raise ConfigError(f"domain at {target!r} could not be imported: {exc}") from exc
        domain = getattr(module, attribute, None)
        if not isinstance(domain, Domain):
            raise ConfigError(f"{target!r} does not name a Domain")
        return domain


#: The process-wide registry.
REGISTRY = DomainRegistry()


def register(domain: Domain) -> None:
    """Register a domain with the process-wide registry."""
    REGISTRY.register(domain)


def get(name: str) -> Domain:
    """Resolve a domain by name or alias."""
    return REGISTRY.get(name)


def names() -> list[str]:
    """Every resolvable domain name."""
    return REGISTRY.names()


def resolve_preset(domain: Domain, preset: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Merge a preset's defaults under explicit keyword arguments.

    Explicit arguments always win, so a preset is a starting point rather than a
    constraint.
    """
    if not preset:
        return kwargs
    if preset not in domain.presets:
        known = ", ".join(sorted(domain.presets)) or "(none)"
        raise ConfigError(f"{domain.name} has no preset {preset!r}. Available: {known}")
    merged = dict(domain.presets[preset])
    merged.update({k: v for k, v in kwargs.items() if v is not None})
    return merged


def merge_skill_dirs(*groups: Iterable[str | Path] | str | Path | None) -> list[str]:
    """Flatten skill-directory arguments, preserving order and dropping blanks."""
    out: list[str] = []
    for group in groups:
        if group is None:
            continue
        items = [group] if isinstance(group, (str, Path)) else list(group)
        for item in items:
            text = str(item)
            if text and text not in out:
                out.append(text)
    return out


__all__ = [
    "ENTRY_POINT_GROUP",
    "REGISTRY",
    "Domain",
    "DomainRegistry",
    "get",
    "merge_skill_dirs",
    "names",
    "register",
    "resolve_preset",
]
