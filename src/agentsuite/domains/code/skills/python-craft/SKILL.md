---
name: python-craft
description: >-
  Use when writing or reviewing any Python. Covers what separates code that
  works from code a team can own: error handling, boundaries, typing, testing,
  and the specific mistakes that survive review because they look fine.
requires: [python]
---

# Python craft

Correct, readable, verified. In that order. Clever is not on the list.

## Decide first: is this a script, a module, or a service?

The right amount of structure differs, and mismatching it is the most common
architectural error.

- **Script** — runs once, read by its author. Flat, top-to-bottom, `if __name__`.
  Do not add a class hierarchy to a 60-line script.
- **Module** — imported by others. Public functions typed and documented, private
  helpers prefixed `_`, no side effects at import time.
- **Service** — long-running. Add config injection, structured logging, health
  checks, graceful shutdown.

Promoting a script to a module means giving it a real interface, not wrapping it
in a class.

## Never at import time

```python
# Wrong: import now costs a network round trip and can fail at collection.
client = SomeClient(api_key=os.environ["API_KEY"])
CONFIG = requests.get("https://...").json()
```

Import must be free. Anything that connects, reads credentials, opens files, or
can raise belongs in a function. This breaks tests, IDE tooling and CLIs
otherwise, and the failure mode is confusing every time.

## Errors

**Never swallow.** `except Exception: pass` is a bug you will spend a day finding.

```python
# Wrong
try:
    result = parse(payload)
except Exception:
    result = None

# Right: catch what you can handle, say what failed, keep the cause.
try:
    result = parse(payload)
except ValueError as exc:
    raise ConfigError(f"malformed payload from {source}: {exc}") from exc
```

- Catch the **narrowest** exception that you can actually do something about.
- Always `raise ... from exc`. Losing the cause makes the traceback useless.
- Error messages name the thing that failed and what to do:
  `f"cannot read {path}: {exc}"`, not `"error reading file"`.
- Define a small exception hierarchy per package with one base class, so callers
  can catch everything from you without catching everything.

A bare `except:` also catches `KeyboardInterrupt` and `SystemExit`. It is never
correct.

## Types

Type hints on every public function. Not for the type checker — for the reader.

```python
def load(path: Path, *, strict: bool = False) -> list[Record]: ...
```

- `X | None` rather than `Optional[X]`; `list[str]` rather than `List[str]`.
- Accept the general (`Iterable`, `Sequence`, `Mapping`), return the concrete
  (`list`, `dict`).
- `from __future__ import annotations` at the top, so annotations cost nothing
  and forward references work.
- Do not annotate what is obvious (`i: int` in a loop). Do annotate anything a
  reader would have to go and check.

## The mistakes that pass review

**Mutable default argument.** Shared across every call, forever.

```python
def add(item, bucket=[]):    # wrong -- one list for the life of the process
def add(item, bucket=None):  # right
    bucket = [] if bucket is None else bucket
```

**Late binding in a loop.** Every closure sees the final value.

```python
fns = [lambda: i for i in range(3)]        # all return 2
fns = [lambda i=i: i for i in range(3)]    # right
```

**Modifying a collection while iterating it.** Iterate a copy, or build a new one.

**`is` for value comparison.** `is` is identity. Use `==` for values; `is` only
for `None`, `True`, `False`.

**`assert` for validation.** Stripped entirely under `python -O`. Never use it to
check user input or invariants that matter — raise.

**Equality on floats.** `0.1 + 0.2 != 0.3`. Use `math.isclose`, or integers for money.

**Path strings.** Use `pathlib`. `os.path.join` on a Windows path with forward
slashes is a class of bug that only shows up on someone else's machine.

**Broad `# type: ignore`.** Name the code: `# type: ignore[arg-type]`.

## Structure

- Functions do one thing. If you are writing a comment to mark a section, that
  section is a function.
- Return early. Nesting past three levels usually means a missing guard clause.
- Prefer a dataclass over a dict with fixed keys — the field names get checked.
- Prefer a module-level function over a class with one method and no state.
- A function with more than three positional parameters wants keyword-only
  arguments (`*` in the signature).

## Tests

A change comes with a test that **fails without it**. Write the failing test
first if you can; at minimum, verify it fails before you accept it as a test.

- Test behaviour, not implementation. A test that breaks on every refactor is a
  liability.
- One assertion of intent per test. The test name says what it proves.
- Exercise the error path: the wrong type, the empty input, the boundary.
- No network, no clock, no randomness — inject them.
- `pytest.mark.parametrize` for tables of cases; do not copy the test five times.

Use `run_tests`, then `lint`, then `typecheck`. Report what they actually said.

## Comments and docstrings

Comments explain **why**. The code already says what.

```python
# Wrong
i += 1  # increment i

# Right
# Retry once: the upstream index is eventually consistent and a fresh write
# is occasionally missing from the first read.
```

Docstring the public surface: what it does, what it raises, and anything
surprising. Skip docstrings on obvious private helpers — a wrong or stale
docstring is worse than none.
