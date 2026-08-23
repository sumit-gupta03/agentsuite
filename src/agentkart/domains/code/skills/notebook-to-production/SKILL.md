---
name: notebook-to-production
description: >-
  Use when moving analysis or model code out of a notebook into a module,
  package or scheduled job, or when asked to productionise a prototype. Covers
  what to extract, what to throw away, and the assumptions notebooks hide.
requires: [python]
---

# Notebook to production

A notebook is a record of exploration. Production code is a thing that runs
unattended and correctly. Converting between them is not copy-paste — the
notebook is carrying hidden state and undeclared assumptions that only work
because a human was watching.

## What notebooks hide

Find each of these before writing any module code.

**Execution order.** A notebook's true dependency graph is the order the cells
were *run*, not the order they appear. Restart the kernel and run top to bottom.
If that fails, the notebook does not describe a reproducible computation and you
must reconstruct the real order first.

**Live state.** Variables defined in a deleted cell, a DataFrame mutated in place
three cells earlier, a connection opened interactively. None of it survives.

**Hardcoded everything.** Paths, dates, credentials, thresholds, magic numbers.
Every one becomes a parameter or a config value.

**Manual steps.** "I fixed that column by hand once." That step must become code
or the pipeline is wrong the first time it runs alone.

**Silent assumptions.** The file was already sorted; the date range happened to
exclude the outage; the categorical had exactly those five values in the sample.

## Extract in this order

1. **Pure functions first.** Anything that maps input to output with no I/O.
   These are testable immediately and are most of the real logic.
2. **I/O to the edges.** Loading and saving become explicit functions with paths
   as parameters, called from one place.
3. **Configuration out.** A dataclass or a config file, never module-level
   constants scattered through the code.
4. **An entry point.** One `main(config)` that composes the above, and a thin CLI.

```python
# core.py -- pure, testable, no I/O
def clean(frame: pd.DataFrame, *, min_amount: int) -> pd.DataFrame: ...
def build_features(frame: pd.DataFrame) -> pd.DataFrame: ...

# io.py -- the edges
def load_orders(path: Path) -> pd.DataFrame: ...
def save_features(frame: pd.DataFrame, path: Path) -> None: ...

# pipeline.py -- composition
def run(config: Config) -> None:
    orders = load_orders(config.input_path)
    features = build_features(clean(orders, min_amount=config.min_amount))
    save_features(features, config.output_path)
```

## What to throw away

- Exploratory cells that answered a question and are not part of the computation
- `display()`, `.head()`, plots — unless a plot is a deliverable, in which case it
  gets its own function that writes to a file
- Commented-out alternatives. Git remembers; the file should not
- `%%time`, `!pip install`, magics
- Anything you cannot explain the purpose of. If nobody knows why a filter is
  there, find out before keeping it — carrying it forward blindly is how a
  one-off hack becomes permanent

## What production adds

**Idempotency.** Running twice produces the same result, not duplicated rows. A
notebook run twice usually appends.

**Error handling.** The notebook stopped at the red cell and a human looked at it.
The job must fail loudly with a useful message, or handle the case.

**Logging instead of print.** Structured, levelled, with enough context to debug
from the log alone.

```python
logger.info("loaded %s rows from %s (%s to %s)", len(frame), path, start, end)
```

**Validation at the boundary.** Row count in an expected band, required columns
present, key unique, no nulls in the join keys. The notebook had a human eyeballing
`.head()`; the job needs assertions.

**Tests.** At least: one test per pure function with a small fixture, and one
end-to-end test on a tiny sample. If the notebook produced a known-good output,
that output is your regression fixture.

**Determinism.** Seeds fixed, sort order explicit, no dependence on dict or file
iteration order.

## Reconciling against the notebook

Before declaring the conversion done, prove the module reproduces the notebook.

```python
# Run both on the same input and compare
pd.testing.assert_frame_equal(
    notebook_output.sort_values(keys).reset_index(drop=True),
    module_output.sort_values(keys).reset_index(drop=True),
    check_dtype=False,
)
```

Differences are informative rather than annoying: they are usually a hidden
assumption you have just made explicit, an execution-order bug in the notebook,
or a genuine bug you introduced. Find out which. Do not adjust the module until
the numbers match without knowing why they differed.

## Structure

```
project/
├── src/package/
│   ├── config.py      dataclass, loaded from file or env
│   ├── io.py          load and save
│   ├── core.py        pure transformations
│   ├── pipeline.py    composition
│   └── cli.py         entry point
├── tests/
│   └── test_core.py
└── notebooks/
    └── 01_exploration.ipynb    kept, clearly marked as exploration
```

Keep the notebook. It is the record of why the code looks like it does, and the
next person will want it. Just make sure nothing imports from it.
