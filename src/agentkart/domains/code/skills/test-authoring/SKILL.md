---
name: test-authoring
description: >-
  Use when writing unit tests, reviewing a test suite, or asked to improve
  coverage. Covers what is worth testing, how to write a test that stays useful
  through a refactor, fixtures and mocking discipline, and why coverage is a
  signal rather than a target.
requires: [testing]
---

# Writing tests

A test suite has one job: **tell you when you have broken something**, quickly and
without lying. Most bad suites fail that in one of two ways — they break when
nothing is wrong, or they pass when something is.

## Before writing anything

Read the code under test, then `run_coverage` to see what is actually unexercised.
Writing tests for already-covered lines to raise a number is waste; the uncovered
branches are where the bugs are.

Then decide what is worth testing, in this order:

1. **The error paths.** Almost always the least tested and the most likely to be
   wrong. What happens on empty input, a missing key, a network failure, a null?
2. **The boundaries.** Zero, one, many. First and last. Just under and just over
   the limit. Off-by-one lives here.
3. **The business rules.** The specific logic someone would notice was wrong.
4. **The happy path.** One test, usually. It is the case you already know works.

Do not test: getters, `__repr__`, framework behaviour, or that a mock you just
configured returns what you configured it to return.

## The shape of a good test

```python
def test_refund_rejects_an_amount_above_the_original_charge() -> None:
    charge = Charge(amount_cents=5_000, status="settled")

    with pytest.raises(RefundError, match="exceeds the original charge"):
        refund(charge, amount_cents=6_000)
```

- **The name states what it proves.** `test_refund_1` tells a reader nothing when
  it fails at 3am. If the name needs "and" twice, it is two tests.
- **Arrange, act, assert** — visibly separated. No assertions in the arrange block.
- **One reason to fail.** Five assertions means the last three never run once the
  first breaks.
- **`match=` on `pytest.raises`.** Without it, the test passes when a completely
  different `RefundError` is raised — including one from a typo.

## Test behaviour, not implementation

The single most important rule, and the one most often broken.

```python
# Brittle: breaks on any refactor, proves nothing about correctness
def test_process_calls_the_validator():
    with patch("module.validate") as mock:
        process(order)
    mock.assert_called_once()

# Useful: survives refactoring, catches real regressions
def test_process_rejects_an_order_with_no_line_items():
    with pytest.raises(ValidationError, match="at least one line item"):
        process(Order(line_items=[]))
```

If a test breaks when you rename a private method or reorder two calls, it was
testing implementation. A suite full of those makes refactoring expensive, which
means the code stops being refactored.

## Parametrise tables of cases

```python
@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "0.00"),
        (1, "0.01"),
        (-1, "-0.01"),
        (100, "1.00"),
        (999_999_99, "999999.99"),
    ],
)
def test_formats_minor_units_as_currency(amount: int, expected: str) -> None:
    assert format_currency(amount) == expected
```

Five tests, one body, and a failure names the exact case. Far better than one test
with five assertions, where you only ever see the first failure.

## Fixtures

```python
@pytest.fixture
def order() -> Order:
    return Order(id="o-1", line_items=[LineItem(sku="A", quantity=2)])
```

- Fixtures build **data and resources**, not assertions.
- Prefer a plain helper function to a fixture when there is no setup or teardown —
  fixtures are indirection, and indirection has a cost in a test.
- `yield` for anything needing cleanup; the teardown runs even when the test fails.
- Keep them local. A fixture in `conftest.py` used by one file belongs in that file.

**Isolation is not optional.** No test may depend on another having run, or on the
order they run in. Shared mutable state between tests produces failures that
appear only in CI, only sometimes.

## Mocking

Mock at the **boundary** — the network, the clock, the filesystem, the payment
provider. Do not mock your own internals.

```python
# Right: the boundary
def test_retries_once_on_a_timeout(monkeypatch):
    calls = []
    def fake_post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise Timeout()
        return Response(200)
    monkeypatch.setattr(client, "post", fake_post)

    assert submit(payload).status == 200
    assert len(calls) == 2
```

Rules that keep mocks honest:

- **Patch where it is used, not where it is defined.** `patch("myapp.service.requests")`,
  not `patch("requests")`. This trips everyone once.
- **`autospec=True`** so the mock rejects calls the real object would reject.
  Without it, a renamed method silently keeps passing.
- **Prefer dependency injection to patching.** A function taking `clock=time.time`
  needs no mock at all — pass a lambda.
- If a test needs five mocks, the code under test has too many dependencies. That
  is a design finding, worth reporting.

## Determinism

A flaky test is worse than no test: it trains the team to re-run CI until it goes
green, and then a real failure gets re-run too.

Never in a test: real network, real clock, `random` without a seed, `sleep`,
dependence on dict or filesystem ordering, or a shared database without isolation.

Freeze time by injecting it, not by patching `datetime` globally.

## Coverage

Coverage tells you what is **not** tested. It says nothing about whether what is
tested is tested well.

```
Name                 Stmts   Miss  Cover   Missing
src/refund.py           82      9    89%   45-48, 91, 103-106
```

Read the **Missing** column, not the percentage. Lines 45-48 are an error branch
nobody exercises; that is the finding. 100% coverage with assertions that never
fail is worth nothing, and chasing a target produces exactly those tests.

Report uncovered *branches and their significance*, not a number.

## Reviewing an existing suite

Look for these, in order of how much damage they do:

| Finding | Why it matters |
|---|---|
| Tests with no assertion | Passes unless the code raises. Very common |
| `assert result` on a non-boolean | Passes for any truthy value, including wrong ones |
| `pytest.raises` with no `match` | Passes on the wrong error |
| Mocks asserting call counts | Tests implementation; breaks on refactor |
| Shared mutable state between tests | Order-dependent, fails only in CI |
| `time.sleep` | Flaky and slow |
| No error-path tests | The most likely bugs are untested |
| A test that has never failed | Verify it can fail — break the code and check |

That last one is the real check on any test you write: **confirm it fails without
the change.** A test that passes both before and after is not a test.

## Reporting

> Added 11 tests to `src/refund.py`, covering the three previously untested error
> branches (lines 45-48, 103-106).
>
> `run_tests`: 47 passed. `run_coverage`: 89% → 96%, with lines 91 and 118
> still uncovered — both are `except OSError` paths I could not trigger without
> a filesystem fault injector.
>
> While writing these I found a real bug: `refund()` accepts a negative amount and
> increases the charge. There was no test because there was no check. I have added
> the failing test; the fix is a one-line guard, which I have not applied since it
> changes behaviour — say the word.
