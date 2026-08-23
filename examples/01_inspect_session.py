"""What the agent actually is, before you spend a token on it.

Run this first. It needs no API key and no warehouse -- it resolves the domain,
the skill library and the tool set, then prints the exact system prompt that
would be sent to the model. If something surprises you later, it was usually
visible here.

    python examples/01_inspect_session.py
"""

from __future__ import annotations

import agentkart as agent


def main() -> None:
    print("=" * 72)
    print("WHAT IS INSTALLED")
    print("=" * 72)
    for name, description in agent.list_domains():
        print(f"  domain  {name:<20} {description}")
    for domain, presets in agent.list_presets().items():
        for preset in presets:
            print(f"  preset  {preset:<20} -> {domain}")

    # A model id string is not resolved until a run happens, so building the
    # agent costs nothing and touches no network.
    de = agent.dataengineering(warehouse="sqlite")

    print()
    print("=" * 72)
    print("RESOLVED SESSION")
    print("=" * 72)
    print(de.describe())

    print()
    print("=" * 72)
    print("SYSTEM PROMPT (sent to the model verbatim)")
    print("=" * 72)
    print(de.system_prompt)

    print()
    print("=" * 72)
    print("NOTE")
    print("=" * 72)
    print(
        "Only skill names and descriptions appear above. Skill bodies are fetched\n"
        "on demand via the load_skill tool -- which is why a large library stays\n"
        "affordable, and why agent.skills_used tells you what actually mattered.\n"
        "\n"
        "The shared rules come from agentkart.core.prompts; the warehouse paragraph\n"
        "comes from the domain. Adding a domain does not touch the core prompt."
    )

    de.close()


if __name__ == "__main__":
    main()
