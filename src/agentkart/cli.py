"""Command line interface.

::

    agentkart de "profile raw.orders"              one-shot against a domain
    agentkart snowflake "list the schemas"         a preset
    agentkart chat --domain de                     interactive session
    agentkart domains                              what is installed
    agentkart skills list --domain de              what is loaded, and from where
    agentkart skills show incremental-backfill
    agentkart tools list --domain de
    agentkart init                                 scaffold ./.agentlib
    agentkart doctor --domain de                   the resolved session + system prompt
    agentkart route "the spark job is skewing"     pick the specialist automatically

The first positional argument is a domain or preset name when it matches one,
otherwise the whole line is treated as a prompt for the default domain.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from .core.errors import AgentError, ConfigError

DEFAULT_DOMAIN = "dataengineering"
SUBCOMMANDS = {"chat", "domains", "skills", "tools", "init", "doctor", "route"}

BANNER = "agentkart -- type /exit to leave, /help for commands"

INIT_CONFIG = """\
# agentkart project configuration
# Committed to the repo. Credentials belong in the environment, not here.

[profile.default]
model = "claude-opus-5"
write = false

  # Domain-specific settings nest under the domain name, so several domains
  # can share one profile without colliding.
  [profile.default.dataengineering]
  warehouse = "duckdb"
  max_rows = 1000

[profile.prod]
write = false

  [profile.prod.dataengineering]
  warehouse = "snowflake"
  max_rows = 500
  max_scan_gb = 50
"""

INIT_SKILL = """\
---
name: house-style
description: >-
  Use for any SQL, model or service this team will own. Covers naming, the
  required audit columns, and which patterns are allowed.
---

# House style

Replace this with your team's actual rules. A few that earn their place:

- Every table carries `loaded_at` (UTC) and `source_system`.
- Timestamps are stored UTC and named with an `_at` suffix. Dates use `_on`.
- Money is stored as integer minor units, never as a float.
- No `SELECT *` outside staging.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentkart", description="One core, many domains.")
    parser.add_argument("prompt", nargs="*", help="Task to run. Omit for an interactive chat.")
    parser.add_argument("-d", "--domain", help=f"Domain or preset (default: {DEFAULT_DOMAIN}).")
    parser.add_argument("-m", "--model", help="Model id (default: claude-opus-5).")
    parser.add_argument("-p", "--profile", help="Config profile to use.")
    parser.add_argument(
        "-s", "--skills", action="append", default=[], help="Extra skill directory (repeatable)."
    )
    parser.add_argument(
        "--disable-skill", action="append", default=[], help="Skill to drop (repeatable)."
    )
    parser.add_argument(
        "--allow-plugins", action="store_true", help="Load third-party skill packs."
    )
    parser.add_argument("--write", action="store_true", help="Permit state-changing actions.")
    parser.add_argument("--max-turns", type=int, help="Turn budget for a single task.")
    parser.add_argument(
        "-o",
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Domain option, e.g. -o warehouse=duckdb -o max_rows=50 (repeatable).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress tool-call progress output.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser


def _parse_options(pairs: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ConfigError(f"--option expects KEY=VALUE, got {pair!r}")
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            options[key.strip()] = lowered == "true"
        elif value.strip().isdigit():
            options[key.strip()] = int(value.strip())
        else:
            options[key.strip()] = value.strip()
    return options


def _agent_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    from .core.loop import confirm_in_terminal

    kwargs: dict[str, Any] = {
        "model": args.model,
        "profile": args.profile,
        "skills": args.skills,
        "disable_skills": args.disable_skill,
        "allow_plugins": args.allow_plugins or None,
        "write": args.write or None,
        "max_turns": args.max_turns,
        "confirm": confirm_in_terminal,
    }
    kwargs.update(_parse_options(args.option))
    return {k: v for k, v in kwargs.items() if v not in (None, [], ())}


def _build_agent(domain: str, args: argparse.Namespace) -> Any:
    import agentkart as package

    factory = getattr(package, domain, None)
    if factory is None:
        raise ConfigError(f"unknown domain or preset {domain!r}. Try: agentkart domains")
    return factory(**_agent_kwargs(args))


def _known_domain(name: str) -> bool:
    import agentkart as package

    from .core import domain as domain_module
    from .core.errors import ConfigError as _ConfigError

    try:
        domain_module.get(name)
        return True
    except _ConfigError:
        pass
    return getattr(package, name, None) is not None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in SUBCOMMANDS:
        command, rest = argv[0], argv[1:]
        handlers = {
            "chat": _cmd_chat,
            "domains": _cmd_domains,
            "skills": _cmd_skills,
            "tools": _cmd_tools,
            "init": _cmd_init,
            "doctor": _cmd_doctor,
            "route": _cmd_route,
        }
        return _guard(handlers[command], rest)

    # A leading bare word that names a domain or preset selects it.
    domain = DEFAULT_DOMAIN
    if argv and not argv[0].startswith("-") and _known_domain(argv[0]):
        domain, argv = argv[0], argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.domain:
        domain = args.domain

    if not args.prompt:
        return _guard(lambda _: interactive(domain=domain, **_agent_kwargs(args)), None)

    return _guard(lambda _: _run_once(domain, args), None)


def _guard(fn: Any, arg: Any) -> int:
    try:
        return int(fn(arg) or 0)
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _run_once(domain: str, args: argparse.Namespace) -> int:
    built = _build_agent(domain, args)
    with built:
        result = built.run(
            " ".join(args.prompt), on_tool=None if args.quiet else _print_tool_call
        )
        print("\n" + result.text)
        if not args.quiet:
            print(_footer(built, result))
    return 0


def _print_tool_call(call: Any) -> None:
    summary = ", ".join(f"{k}={_short(v)}" for k, v in call.input.items())
    print(f"  -> {call.name}({summary})", file=sys.stderr)


def _short(value: Any, limit: int = 60) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _footer(built: Any, result: Any) -> str:
    bits = [
        f"{result.turns} turn(s)",
        f"{result.usage.input_tokens + result.usage.output_tokens:,} tokens",
    ]
    if result.usage.cache_read_tokens:
        bits.append(f"{result.usage.cache_read_tokens:,} cached")
    if built.skills_used:
        bits.append("skills: " + ", ".join(built.skills_used))
    return "\n-- " + " | ".join(bits)


def interactive(domain: str = DEFAULT_DOMAIN, **kwargs: Any) -> int:
    """Run a REPL session. Backs both ``agent chat`` and ``agent.start()``."""
    import agentkart as package

    from .core.loop import confirm_in_terminal

    kwargs.setdefault("confirm", confirm_in_terminal)
    factory = getattr(package, domain, None)
    if factory is None:
        raise ConfigError(f"unknown domain or preset {domain!r}. Try: agentkart domains")
    built = factory(**kwargs)

    print(BANNER)
    print(f"   {built!r}\n")

    with built:
        while True:
            try:
                line = input("agentkart> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                return 0
            if line == "/help":
                print(
                    "  /skills   list resolved skills and their sources\n"
                    "  /tools    list available tools\n"
                    "  /actions  show everything done this session\n"
                    "  /session  show the resolved configuration\n"
                    "  /reset    clear the conversation\n"
                    "  /exit     leave"
                )
                continue
            if line == "/skills":
                for skill in built.skills.values():
                    print(f"  {skill.name:<28} [{skill.source}] {skill.description[:60]}")
                continue
            if line == "/tools":
                print("  " + ", ".join(built.tools.names()))
                continue
            if line == "/actions":
                for record in built.actions:
                    print(f"  [{record.tier}] {record.detail}")
                if not built.actions:
                    print("  (none)")
                continue
            if line == "/session":
                print(built.describe())
                continue
            if line == "/reset":
                built.reset()
                print("  conversation cleared")
                continue

            try:
                result = built.run(line, on_tool=_print_tool_call)
            except AgentError as exc:
                print(f"  error: {exc}", file=sys.stderr)
                continue
            print("\n" + result.text)
            print(_footer(built, result) + "\n")


def _cmd_chat(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return interactive(domain=args.domain or DEFAULT_DOMAIN, **_agent_kwargs(args))


def _cmd_route(argv: list[str]) -> int:
    """agent route "<request>" -- pick the specialist, then run it."""
    import agentkart as package

    args = _build_parser().parse_args(argv)
    request = " ".join(args.prompt)

    router = package.auto(**_agent_kwargs(args))

    if not request:
        print(router.describe())
        return 0

    decision = router.select(request)
    print(f"-> {decision}", file=sys.stderr)

    built = router.agent_for(decision.preset)
    with router:
        result = built.run(request, on_tool=None if args.quiet else _print_tool_call)
        print("\n" + result.text)
        if not args.quiet:
            print(_footer(built, result))
    return 0


def _cmd_domains(argv: list[str]) -> int:
    import agentkart as package

    listed = package.list_domains()
    if not listed:
        print("(no domains installed)")
        return 0
    width = max(len(name) for name, _ in listed)
    print("DOMAINS -- bring tools, a policy and a skill library\n")
    for name, description in listed:
        print(f"  {name:<{width}}  {description}")

    grouped = package.list_presets()
    if grouped:
        print("\nPRESETS -- a domain plus configuration, no extra code\n")
        for domain_name, names in grouped.items():
            print(f"  {domain_name}:")
            for preset in names:
                print(f"    agent {preset:<12} ==  agent {domain_name} (preset={preset!r})")
    return 0


def _cmd_skills(argv: list[str]) -> int:
    """agent skills [list|show <name>|path] [flags]"""
    from .core.skills import SOURCE_ORDER

    # The parser's positional `prompt` absorbs the sub-action and its argument,
    # so flags and their values stay intact -- filtering argv by hand would
    # silently drop the value after -d.
    args = _build_parser().parse_args(argv)
    words = args.prompt
    action = words[0] if words else "list"
    domain = args.domain or DEFAULT_DOMAIN

    if action == "path":
        from .core.skills import USER_DIR, find_project_skills_dir

        print(f"user:    {USER_DIR / 'skills'}")
        print(f"         {USER_DIR / 'skills' / domain}   (domain-specific)")
        project = find_project_skills_dir()
        print(f"project: {project or '(none found)'}")
        if project:
            print(f"         {project / domain}   (domain-specific)")
        return 0

    built = _build_agent(domain, args)

    if action == "show":
        if len(words) < 2:
            print("usage: agentkart skills show <name>", file=sys.stderr)
            return 2
        from .core import domain as domain_module
        from .core.skills import resolve_skills

        resolved = domain_module.get(domain)
        catalogue = resolve_skills(
            bundled_dir=resolved.skills_dir,
            domain=resolved.name,
            extra_dirs=args.skills,
            allow_plugins=args.allow_plugins,
            capabilities=None,
        )
        skill = catalogue.get(words[1])
        if skill is None:
            known = ", ".join(catalogue) or "(none)"
            print(f"no skill named {words[1]!r} in {domain}. Available: {known}", file=sys.stderr)
            return 1
        print(f"# {skill.name}\n")
        print(f"source:      {skill.source}")
        print(f"path:        {skill.path}")
        print(f"description: {skill.description}")
        if skill.requires:
            print(f"requires:    {', '.join(skill.requires)}")
        files = skill.list_files()
        if files:
            print(f"files:       {', '.join(files)}")
        print("\n" + "-" * 70 + "\n")
        print(skill.body)
        return 0

    # Discovery command: show the whole library, not only what this session
    # activates. `agent doctor` reports what is actually live.
    from .core import domain as domain_module
    from .core.skills import resolve_skills

    resolved = domain_module.get(domain)
    catalogue = resolve_skills(
        bundled_dir=resolved.skills_dir,
        domain=resolved.name,
        extra_dirs=args.skills,
        allow_plugins=args.allow_plugins,
        capabilities=None,
    )
    if not catalogue:
        print("(no skills resolved)")
        return 0

    active = set(built.skills)
    width = max(len(name) for name in catalogue)
    print(f"{len(catalogue)} skill(s) for {resolved.name}")
    print(f"precedence: {' < '.join(SOURCE_ORDER)}\n")
    for skill in catalogue.values():
        gate = "" if skill.name in active else f"  (needs: {', '.join(skill.requires)})"
        print(f"  {skill.name:<{width}}  [{skill.source:<14}] {skill.description}{gate}")
    if len(catalogue) != len(active):
        print(
            f"\n{len(active)} active in this session; the rest need a capability "
            "this session does not have (connect a warehouse, point at a dbt project)."
        )
    return 0


def _cmd_tools(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    built = _build_agent(args.domain or DEFAULT_DOMAIN, args)
    with built:
        for spec in sorted(built.tools, key=lambda s: s.name):
            flag = " [destructive]" if spec.destructive else ""
            print(f"  {spec.name}{flag}\n      {spec.description}\n")
    return 0


def _cmd_init(argv: list[str]) -> int:
    words = _build_parser().parse_args(argv).prompt
    root = Path(words[0]) if words else Path.cwd()
    base = root / ".agentlib"
    skills_dir = base / "skills" / "house-style"
    skills_dir.mkdir(parents=True, exist_ok=True)

    config_path = base / "config.toml"
    if config_path.exists():
        print(f"{config_path} already exists; leaving it alone")
    else:
        config_path.write_text(INIT_CONFIG, encoding="utf-8")
        print(f"created {config_path}")

    skill_path = skills_dir / "SKILL.md"
    if skill_path.exists():
        print(f"{skill_path} already exists; leaving it alone")
    else:
        skill_path.write_text(INIT_SKILL, encoding="utf-8")
        print(f"created {skill_path}")

    print(
        "\nSkills in .agentlib/skills/ apply to every domain."
        "\nPut domain-specific ones in .agentlib/skills/<domain>/."
        "\nBoth override bundled skills by name. Commit them."
    )
    return 0


def _cmd_doctor(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    built = _build_agent(args.domain or DEFAULT_DOMAIN, args)
    with built:
        print(built.describe())
        print("\n--- system prompt " + "-" * 52 + "\n")
        print(built.system_prompt)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
