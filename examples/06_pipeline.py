"""The actual flow: import once, call agents in a pipeline.

Each agent is an ordinary Python object. Compose them however your pipeline
composes anything else -- sequentially, conditionally, in a loop, in a DAG task.
There is no framework to submit to.

    python examples/06_pipeline.py            # no API key: shows the wiring
    ANTHROPIC_API_KEY=... python examples/06_pipeline.py --live
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import agentkart as agent

HOUSE_STYLE = """\
---
name: house-style
description: >-
  Use for any code this team will own. Our naming rules, required audit columns
  and logging conventions.
---

# House style

- Money is stored as integer minor units, never a float.
- Every table carries `loaded_at` (UTC) and `source_system`.
- Log with `logger`, never `print`.
"""


def confirm_in_ci(action: str, detail: str, purpose: str) -> bool:
    """Your confirmation gate. Destructive actions are refused unless this says yes.

    In a real pipeline this asks Slack, checks a change ticket, or returns False
    outside a maintenance window. Returning True unconditionally defeats the point.
    """
    print(f"    [confirm] {action} -- {purpose}", file=sys.stderr)
    return False


def build_project(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / ".agentlib" / "skills" / "house-style").mkdir(parents=True)
    (root / ".agentlib" / "skills" / "house-style" / "SKILL.md").write_text(
        HOUSE_STYLE, encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "etl"\ndependencies = ["pyspark", "scikit-learn"]\n', encoding="utf-8"
    )
    (root / "src" / "transform.py").write_text(
        "from pyspark.sql import functions as F\n"
        "\n"
        "def clean(df):\n"
        "    # Drops nulls silently -- see pyspark-correctness\n"
        "    return df.filter(F.col('status') != 'cancelled')\n",
        encoding="utf-8",
    )


def main() -> int:
    live = "--live" in sys.argv
    if live and not os.environ.get("ANTHROPIC_API_KEY"):
        print("--live needs ANTHROPIC_API_KEY (or an `ant auth login` profile).", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "etl"
        build_project(root)

        # ---------------------------------------------------------------
        # Build the agents. Nothing has happened yet -- no connection, no
        # API call. Construction is cheap and inspectable.
        # ---------------------------------------------------------------
        reviewer = agent.pyspark(project=root)                     # read-only
        analyst = agent.dataengineering(warehouse="sqlite")        # read-only
        author = agent.pyspark(                                    # can write
            project=root,
            write=True,
            confirm=confirm_in_ci,
            audit_path=root / "audit.jsonl",
        )
        tester = agent.testing(                                    # writes tests
            project=root,
            write=True,
            confirm=confirm_in_ci,
            audit_path=root / "audit.jsonl",
        )

        stages = (
            ("reviewer", reviewer),
            ("analyst", analyst),
            ("author", author),
            ("tester", tester),
        )
        for name, built in stages:
            print(f"{name:<9} {built!r}")
            print(f"          skills: {', '.join(built.skills)}")
            print(f"          tools:  {len(built.tools)}")

        print()
        print("Note house-style appears in every code agent: it came from")
        print(".agentlib/skills/ in the project, which applies to all domains.")
        print()
        print("Note the reviewer has no write_file tool at all -- capability")
        print("gating removes it, rather than registering it to fail later.")

        if not live:
            print()
            print("=" * 70)
            print("Add --live (with credentials) to actually run these.")
            print("=" * 70)
            print(
                "\nThe pipeline would then be ordinary Python:\n"
                "\n"
                "    findings = reviewer.run('Review src/transform.py for correctness bugs.')\n"
                "\n"
                "    if 'null' in findings.text.lower():\n"
                "        fix   = author.run(f'Fix this:\\n{findings.text}')\n"
                "        tests = tester.run(f'Write tests for that fix:\\n{fix.text}')\n"
                "\n"
                "        if 'failed' in tests.text:\n"
                "            author.run(f'Your fix fails the new tests:\\n{tests.text}')\n"
                "\n"
                "Each .run() returns a RunResult with .text, .turns, .usage and\n"
                "the tool calls made. Conversation persists on the agent, so a\n"
                "second .run() continues the same session."
            )
            for _, built in stages:
                built.close()
            return 0

        # ---------------------------------------------------------------
        # Live: the model chooses which skills to load and which tools to
        # call. Nothing below tells it how.
        # ---------------------------------------------------------------
        print("\n" + "=" * 70)
        print("STAGE 1 -- review (read-only)")
        print("=" * 70)
        findings = reviewer.run(
            "Review src/transform.py for correctness bugs. Be specific about what breaks.",
            on_tool=lambda c: print(f"  -> {c.name}", file=sys.stderr),
        )
        print(findings.text)
        print(f"\nskills the model chose: {', '.join(reviewer.skills_used) or '(none)'}")

        print("\n" + "=" * 70)
        print("STAGE 2 -- fix (write-enabled, gated)")
        print("=" * 70)
        fix = author.run(
            f"Apply this review to src/transform.py, then lint it:\n\n{findings.text}",
            on_tool=lambda c: print(f"  -> {c.name}", file=sys.stderr),
        )
        print(fix.text)

        print("\n" + "=" * 70)
        print("STAGE 3 -- test (the testing agent, same pipeline)")
        print("=" * 70)
        tests = tester.run(
            "Write unit tests covering the bug just fixed in src/transform.py, "
            f"then run them and report coverage:\n\n{fix.text}",
            on_tool=lambda c: print(f"  -> {c.name}", file=sys.stderr),
        )
        print(tests.text)
        print(f"\nskills the model chose: {', '.join(tester.skills_used) or '(none)'}")

        print("\n" + "=" * 70)
        print(author.governance_report())

        for _, built in stages:
            built.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
