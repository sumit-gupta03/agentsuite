# Writing skills

A skill is a directory with a `SKILL.md` file. That is the whole format.

```
incremental-backfill/
├── SKILL.md              required
├── reference/            optional supporting documents
│   └── reconciliation.md
└── scripts/              optional example code
    └── validate.py
```

## Frontmatter

```markdown
---
name: incremental-backfill
description: >-
  Use when backfilling an incremental table, handling late-arriving data, or
  deciding between an incremental load and a full refresh.
requires: [warehouse]
---

# Incremental backfill

...body...
```

| Field | Required | Meaning |
|---|---|---|
| `name` | no | Defaults to the directory name. Used for overriding and for `load_skill`. |
| `description` | **yes** | The only text the model sees before deciding to load the skill. |
| `requires` | no | Capability names. The skill is hidden when any is absent. |

Anything else you put in the frontmatter lands in `skill.metadata` and is
ignored by the runtime — useful for your own tooling (`owner`, `version`,
`reviewed_on`).

## Writing the description

This is the highest-leverage line in the file, and the one people get wrong.

The description is loaded into the system prompt for *every* skill, every turn.
The body is not. So the description is doing one job: helping the model decide
whether this skill is relevant right now.

Write it as **when to use this**, not **what this is about**.

```yaml
# Weak — describes the topic
description: Documentation about incremental models and backfills.

# Strong — describes the trigger
description: >-
  Use when backfilling an incremental table, handling late-arriving data, or
  deciding between an incremental load and a full refresh. Covers watermark
  selection, idempotency, and partition-safe reruns.
```

Include the words someone would actually use for the task. If your team says
"reload" rather than "backfill", put both in.

Aim for one to three sentences. A description short enough to be vague costs you
a skill that never fires; one long enough to be an essay costs you prompt budget
on every turn of every session.

## Writing the body

The body is read only after the model has committed to the skill, so it can be
as long as the subject deserves. Some things that make bodies work:

- **Lead with the decision.** Most skills exist because someone has to choose
  between two approaches. Put that choice first, with the criteria.
- **Be specific and opinionated.** "Consider your lookback window carefully" is
  worthless. "Set the lookback past the p99.9 of the arrival lag, and re-measure
  quarterly" is a skill.
- **Include the failure modes.** A table of symptom → usual cause is often the
  most-used part of a skill.
- **Show the SQL.** Concrete, runnable, with the trap called out in a comment.
- **Name the tools.** `Run find_duplicates before trusting the merge key` ties
  the guidance to something the agent can actually do.

Skills are prose that *describes*. They cannot make anything happen — only
registered tools can, and tools enforce policy regardless of what a skill says.
Write accordingly: a skill that says "drop the table and rebuild" will still be
refused by the guardrail layer unless the session permits it.

## `requires:` capabilities

Available capability names:

| Capability | Present when |
|---|---|
| `skills` | always |
| `warehouse` | a warehouse is connected |
| `write` | the session was constructed with `write=True` |
| `dbt` | `dbt_project_dir` points at a real dbt project |
| dialect name | e.g. `snowflake`, `bigquery`, `duckdb`, `postgres`, `sqlite` |

`requires: [snowflake]` gives you dialect-specific skills that stay out of the
prompt everywhere else.

## Overriding a bundled skill

Give your skill the same `name` and put it in a higher-precedence location:

```
your-repo/.agentlib/skills/sql-review/SKILL.md
```

That replaces the packaged `sql-review` entirely — it is a replacement, not a
merge. Confirm it took effect:

```bash
agentkart skills list        # source column should read [project]
```

To remove a bundled skill without replacing it:

```python
agent.dataengineering(disable_skills=["sql-review"])
```

## Supporting files

Files alongside `SKILL.md` are listed to the model when the skill loads, and
readable with `read_skill_file`. Use them for material that is valuable but too
long to justify loading every time the skill fires — reference queries, worked
examples, a decision table.

Reference them from the body so the model knows they exist and why it would
want one:

```markdown
See `reference/reconciliation.md` for the diff queries.
```

Paths are sandboxed to the skill directory; `../` is refused.

## Shipping a skill pack

Any package can advertise skills through an entry point:

```toml
# pyproject.toml of agent-skills-databricks
[project.entry-points."agentkart.skills"]
databricks = "de_skills_databricks:SKILLS_DIR"
```

```python
# de_skills_databricks/__init__.py
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
```

The entry point may resolve to a path, or to a module — in which case
`<module dir>/skills` is used.

**Packs are not loaded unless the user opts in** with `allow_plugins=True` (or
`--allow-plugins`). This is deliberate: an installed package should not be able
to inject instructions into someone's agent silently. Say so in your pack's
README so users know the flag is needed.

A pack that fails to import, or contains a malformed skill, logs a warning and
is skipped. One broken pack never takes down the agent.

## Checking your work

```bash
agentkart skills list                  # is it there, and from where?
agentkart skills show my-skill         # frontmatter, files, full body
agentkart skills path                  # where each source resolves to
agentkart doctor                       # the exact system prompt
```

After a run, `agent.skills_used` tells you which skills the model actually
loaded. If a skill you expected never appears there, the description is the
thing to fix — not the body.
