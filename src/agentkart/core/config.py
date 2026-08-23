"""Configuration and profile resolution.

Precedence, lowest to highest::

    package defaults
    ~/.agentlib/config.toml     [profile.<name>] and [profile.<name>.<domain>]
    ./.agentlib/config.toml     same
    AGENT_* environment variables
    keyword arguments

Core settings are the ones every domain shares -- model, turn budget, write flag,
skill directories. Anything domain-specific lives in ``Config.options``, so
adding a domain never means adding fields here.

Credentials are deliberately not modelled. Domains read them from the
environment or the provider's own credential chain, so a config file committed by
accident leaks a hostname, not a password.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

# tomllib landed in 3.11; on 3.10 fall back to tomli if it happens to be there.
_toml: Any = None
try:
    import tomllib

    _toml = tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    try:
        import tomli

        _toml = tomli
    except ModuleNotFoundError:  # pragma: no cover
        _toml = None


CONFIG_DIRNAME = ".agentlib"
CONFIG_FILENAME = "config.toml"
USER_CONFIG_DIR = Path.home() / CONFIG_DIRNAME

#: Core fields. Anything else in a config section is a domain option.
CORE_FIELDS = {
    "profile",
    "domain",
    "model",
    "max_tokens",
    "effort",
    "write",
    "max_turns",
    "skill_dirs",
    "disable_skills",
    "allow_plugins",
    "options",
}


@dataclass
class Config:
    """A resolved session configuration."""

    profile: str = "default"
    domain: str | None = None

    # model
    model: str = "claude-opus-5"
    #: None means "whatever this backend's default is". A number here is an
    #: explicit instruction, and providers cap it differently -- Nova Pro at
    #: 10k, Claude far higher -- so a shared default would fail on some.
    max_tokens: int | None = None
    effort: str = "high"

    # policy
    write: bool = False

    # loop
    max_turns: int = 25

    # skills
    skill_dirs: list[str] = field(default_factory=list)
    disable_skills: list[str] = field(default_factory=list)
    allow_plugins: bool = False

    #: Domain-specific settings -- warehouse, project_root, region, and so on.
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, name: str, default: Any = None) -> Any:
        """Read a domain option."""
        return self.options.get(name, default)

    def merged(self, **overrides: Any) -> Config:
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(clean) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise ConfigError(f"unknown config option(s): {', '.join(sorted(unknown))}")
        return replace(self, **clean)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    if _toml is None:  # pragma: no cover - 3.10 without tomli
        raise ConfigError(
            f"cannot read {path}: TOML support requires Python 3.11+ or the 'tomli' package"
        )
    try:
        with path.open("rb") as handle:
            return _toml.load(handle)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"cannot parse {path}: {exc}") from exc


def _profile_section(document: dict[str, Any], profile: str, domain: str | None) -> dict[str, Any]:
    """Read ``[profile.<name>]`` plus its ``[profile.<name>.<domain>]`` subsection.

    Domain subsections let one profile configure several domains without their
    settings colliding.
    """
    profiles = document.get("profile")
    section: dict[str, Any]
    if isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
        section = dict(profiles[profile])
    else:
        section = {k: v for k, v in document.items() if k != "profile"}

    nested = {k: v for k, v in section.items() if isinstance(v, dict)}
    flat = {k: v for k, v in section.items() if not isinstance(v, dict)}

    options: dict[str, Any] = {k: v for k, v in flat.items() if k not in CORE_FIELDS}
    resolved: dict[str, Any] = {k: v for k, v in flat.items() if k in CORE_FIELDS}

    if domain and isinstance(nested.get(domain), dict):
        for key, value in nested[domain].items():
            if key in CORE_FIELDS:
                resolved[key] = value
            else:
                options[key] = value

    if options:
        resolved["options"] = options
    return resolved


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``.agentlib/config.toml``."""
    current = (start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - no home directory
        home = None

    for directory in (current, *current.parents):
        if home is not None and directory == home:
            break  # the home directory is the *user* tier, not a project
        candidate = directory / CONFIG_DIRNAME / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _from_env() -> dict[str, Any]:
    mapping: dict[str, Any] = {}

    def take(env: str, key: str, cast: Any = str) -> None:
        raw = os.environ.get(env)
        if raw is None or raw == "":
            return
        if cast is bool:
            mapping[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            try:
                mapping[key] = cast(raw)
            except ValueError as exc:
                raise ConfigError(f"{env}={raw!r} is not a valid {cast.__name__}") from exc

    take("AGENT_PROFILE", "profile")
    take("AGENT_DOMAIN", "domain")
    take("AGENT_MODEL", "model")
    take("AGENT_MAX_TOKENS", "max_tokens", int)
    take("AGENT_EFFORT", "effort")
    take("AGENT_WRITE", "write", bool)
    take("AGENT_MAX_TURNS", "max_turns", int)
    take("AGENT_ALLOW_PLUGINS", "allow_plugins", bool)

    skill_dirs = os.environ.get("AGENT_SKILL_DIRS")
    if skill_dirs:
        mapping["skill_dirs"] = [p for p in skill_dirs.split(os.pathsep) if p]

    disabled = os.environ.get("AGENT_DISABLE_SKILLS")
    if disabled:
        mapping["disable_skills"] = [s.strip() for s in disabled.split(",") if s.strip()]

    # AGENT_OPT_<NAME> sets a domain option, so a domain never needs its own
    # environment plumbing added to this module.
    options = {
        key[len("AGENT_OPT_") :].lower(): value
        for key, value in os.environ.items()
        if key.startswith("AGENT_OPT_") and value
    }
    if options:
        mapping["options"] = options

    return mapping


def load_config(
    profile: str | None = None,
    *,
    domain: str | None = None,
    cwd: Path | None = None,
    user_config: Path | None = None,
    options: dict[str, Any] | None = None,
    **overrides: Any,
) -> Config:
    """Resolve a :class:`Config` from files, environment and keyword overrides."""
    env = _from_env()
    name = profile or overrides.get("profile") or env.get("profile") or "default"
    resolved_domain = domain or overrides.get("domain") or env.get("domain")

    user_path = user_config if user_config is not None else USER_CONFIG_DIR / CONFIG_FILENAME
    project_path = find_project_config(cwd)

    layered: dict[str, Any] = {}
    merged_options: dict[str, Any] = {}

    for source in (
        _profile_section(_read_toml(user_path), name, resolved_domain),
        _profile_section(_read_toml(project_path), name, resolved_domain) if project_path else {},
        env,
        {k: v for k, v in overrides.items() if v is not None},
    ):
        merged_options.update(source.pop("options", {}) or {})
        layered.update(source)

    merged_options.update(options or {})

    layered["profile"] = name
    if resolved_domain:
        layered["domain"] = resolved_domain
    if merged_options:
        layered["options"] = merged_options

    known = {f.name for f in Config.__dataclass_fields__.values()}
    unknown = set(layered) - known
    if unknown:
        raise ConfigError(
            f"unknown config option(s): {', '.join(sorted(unknown))}. Core options: "
            f"{', '.join(sorted(known))}. Domain-specific settings belong in [profile.x.<domain>]."
        )
    return Config(**layered)


__all__ = [
    "CONFIG_DIRNAME",
    "CONFIG_FILENAME",
    "CORE_FIELDS",
    "Config",
    "find_project_config",
    "load_config",
]
