# Contributing

## Setup

```bash
git clone <repo>
cd <repo>
python -m pip install -e ".[dev]"
pytest
```

The suite needs no database, no driver, no network and no API key. If a change
makes that untrue, the change is wrong.

## Before opening a pull request

```bash
pytest
ruff check src tests examples
mypy src --no-site-packages
```

All three must be clean.

## What goes where

This is the decision that keeps the library small. Ask it in this order:

| You want to add | Where it goes | Cost |
|---|---|---|
| Knowledge, conventions, failure modes | A **skill** (`SKILL.md`) | prose |
| A domain pinned to one configuration | A **preset** (dict entry) | one line |
| A new capability the agent lacks | A **tool** (function + schema) | small |
| Tools *and* a permission classifier *and* skills | A **domain** | ~1,500 lines |

**Do not add a domain when a preset will do.** PySpark, PyTorch, RAG, ML,
testing and Terraform are all the *same* domain, because they need the same tools
— read a file, write a file, run a script, run the tests. What differs is
knowledge, and knowledge belongs in skills.

**Nothing you add should require editing `agent/core`.** If it does, the
abstraction is wrong: raise it as an issue rather than widening the core, and we
will find the hook it is missing.

## Writing a skill

See [docs/SKILLS.md](docs/SKILLS.md). The short version:

- The `description` is the highest-leverage line. It is the only text the model
  sees before deciding to load the skill. Write it as *when to use this*, not
  *what this is about*.
- Be specific and opinionated. "Consider your lookback window carefully" is
  worthless; "set it past the p99.9 of arrival lag, and re-measure quarterly" is
  a skill.
- Include the failure modes. A symptom → cause table is usually the most-used
  part.
- Skills describe; they cannot act. Anything a skill tells the agent to do is
  still subject to the permission layer.

## Writing a tool

- Write the JSON schema by hand. The description a model reads is as much a part
  of the interface as the signature.
- Tools receive an `AgentContext` and nothing else — no agent, no model, no API
  client. That boundary is what stops a tool reaching around the policy layer.
- Anything that changes state goes through `context.policy.check(...)` and, if
  destructive, `context.confirm(...)`. Do not add a second route.
- Return a refusal as a `ToolError`; the loop turns it into an error result the
  model can recover from.
- Never interpolate a model-supplied string into a shell. Commands are argv
  lists.

## Security-sensitive changes

Changes to `agent/core/policy.py`, `agent/core/untrusted.py`,
`agent/domains/code/workspace.py` or any allowlist need:

- a test showing the new thing is permitted, **and**
- a test showing the adjacent thing is still refused.

A widened allowlist without a test proving what is still denied will not be
merged. See [SECURITY.md](SECURITY.md) for the threat model.

## Tests

- Test behaviour, not implementation. A test that breaks on a rename was testing
  the wrong thing.
- Confirm a new test can fail. Break the code, watch it go red, put it back.
- No network, no real clock, no unseeded randomness, no `sleep`.
- The agent tests drive the real loop through a scripted fake model
  (`tests/conftest.py`). Use it rather than mocking internals.

## Style

- `ruff` and `mypy` settings live in `pyproject.toml`; do not relax them in a
  pull request that is about something else.
- Comments explain **why**. The code already says what.
- Match the surrounding code. Do not reformat files your change does not touch.

## Reporting bugs

Include the version, what you expected, what happened, and a minimal
reproduction. For anything security-related, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.
