"""Extending the library, in increasing order of cost.

1. A **skill file** -- prose. Free, no code.
2. A **preset** -- a domain plus configuration. A dict entry.
3. A **tool** -- new capability. A function plus a schema.
4. A **domain** -- new tools, policy and skills. Only when the tools differ.

Most of what people want is 1 or 2. Needs no API key: it builds the agents and
inspects them rather than running them.

    python examples/04_extending.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import agentkart as agent
from agentkart.core.domain import Domain
from agentkart.core.policy import Action, Policy

# --- 1. A skill file -------------------------------------------------------
# In a real project this lives at .agentlib/skills/<name>/SKILL.md (all domains)
# or .agentlib/skills/<domain>/<name>/SKILL.md (one domain), committed to the repo.

SKILL = """\
---
name: house-style
description: >-
  Use for any SQL or model this team will own. Covers our naming rules, the
  required audit columns, and which materialisations are permitted.
---

# House style

- Every table carries `loaded_at` (UTC) and `source_system`.
- Money is stored as integer minor units, never as a float.
- No `SELECT *` outside staging models.
"""

# Same name as a bundled skill, so it replaces it entirely.
OVERRIDE = """\
---
name: sql-review
description: >-
  Use when reviewing SQL. Our checklist, shorter than the default because our
  warehouse enforces most of it.
---

# SQL review, our version

1. Join grain -- state the expected output grain before running.
2. Half-open date ranges. Never BETWEEN on a timestamp.
"""


# --- 3. A tool -------------------------------------------------------------
# Tools are the only things that can act. The schema is written by hand because
# the description a model reads is as much a part of the interface as the
# signature.


@agent.tool(
    name="check_pipeline_status",
    description=(
        "Return the last run state of a named pipeline from the orchestrator. "
        "Use this when asked why data is missing or stale, before assuming the "
        "SQL is at fault."
    ),
    schema={
        "type": "object",
        "properties": {
            "pipeline": {"type": "string", "description": "The pipeline or DAG name."}
        },
        "required": ["pipeline"],
    },
)
def check_pipeline_status(context, pipeline: str) -> str:  # noqa: ARG001
    # A real implementation would call Airflow, Dagster, Prefect, etc.
    return f"{pipeline}: last run 2026-08-23T02:00Z, state=success, duration=412s"


# --- 4. A domain -----------------------------------------------------------
# The expensive one. Shown here so the cost is visible: a policy classifier, a
# tool module list, and a skills directory. Nothing in agent.core changes.


class CloudPolicy(Policy):
    """What a cloud domain's classifier would look like."""

    def classify(self, request: str, **context: object) -> list[Action]:
        verb = request.split()[0].lower() if request.split() else ""
        if verb in {"describe", "list", "get"}:
            return [Action("cloud", request, "read", verb.upper())]
        if verb in {"create", "update", "tag"}:
            return [Action("cloud", request, "write", verb.upper())]
        if verb in {"delete", "terminate"}:
            return [Action("cloud", request, "destructive", verb.upper(), "removes resources")]
        return [Action("cloud", request, "destructive", "UNKNOWN", "unrecognised operation")]


CLOUD_DOMAIN = Domain(
    name="cloud",
    description="cloud infrastructure and cost",
    package="agent.core",  # a real one would point at its own package
    policy_factory=lambda config: CloudPolicy(write=config.write),
    aliases=("infra",),
    presets={"aws": {"provider": "aws"}, "gcp": {"provider": "gcp"}},
)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp) / "skills"
        for name, body in (("house-style", SKILL), ("sql-review", OVERRIDE)):
            directory = skills_dir / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(body, encoding="utf-8")

        # 1 + 3: skills and a tool
        de = agent.dataengineering(warehouse="sqlite", skills=skills_dir)
        de.tools.add(check_pipeline_status.__tool_spec__)

        print("1. SKILLS")
        print("-" * 72)
        for skill in de.skills.values():
            marker = "  <- yours" if skill.source == "explicit" else ""
            print(f"   {skill.name:<24} [{skill.source}]{marker}")
        print("\n   sql-review now reads [explicit], not [bundled]. Your file replaced")
        print("   the packaged one by name -- no fork, no monkeypatching.\n")

        # 2: a preset
        print("2. PRESETS  (a domain plus configuration, zero code)")
        print("-" * 72)
        for domain, names in agent.list_presets().items():
            print(f"   {domain}: {', '.join(names)}")
        print("   agent.snowflake()  ==  agent.dataengineering(warehouse='snowflake')\n")

        print("3. TOOLS")
        print("-" * 72)
        print("   " + ", ".join(de.tools.names()))
        print("\n   check_pipeline_status is registered and offered to the model.\n")

        de.close()

        # 4: a domain
        agent.register_domain(CLOUD_DOMAIN)
        print("4. DOMAINS  (the expensive one -- only when the tools differ)")
        print("-" * 72)
        for name, description in agent.list_domains():
            print(f"   {name:<20} {description}")
        print("\n   'cloud' was registered at runtime. Nothing in agent.core changed;")
        print("   a real one would ship its own tools, skills and pip extra.")


if __name__ == "__main__":
    main()
