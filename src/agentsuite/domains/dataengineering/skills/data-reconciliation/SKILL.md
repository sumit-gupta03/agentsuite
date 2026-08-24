---
name: data-reconciliation
description: >-
  Use when asked whether two datasets agree - after a migration, a backfill, a
  pipeline rebuild, or when finance says the numbers do not tie out. Covers what
  to compare and in what order, why counts agreeing proves nothing, and how to
  localise a discrepancy to its cause.
requires: [warehouse]
---

# Reconciliation

The job is not "are these different" — it is **where, by how much, and since
when**. A reconciliation that ends at "the totals do not match" has not started.

## Order of work

1. **`compare_tables`** — counts, distinct keys, keys present on one side only,
   and a checksum. Establishes *what kind* of difference you have.
2. **`compare_by_period`** — buckets the difference by day. A discrepancy
   confined to a date range usually names the incident.
3. **`profile_table` / `find_duplicates`** on whichever side looks wrong.
4. **Read actual differing rows.** Ten of them will normally tell you the cause.

Do not skip to step 4. Without steps 1 and 2 you are looking at ten rows out of
millions and guessing.

## Counts agreeing proves nothing

The single most important thing here.

If the target duplicated 400 rows and dropped 400 others, the row count is
identical and the data is wrong. This is not a hypothetical — it is what a
non-unique merge key does.

Always check three things together:

| Check | Catches |
|---|---|
| `COUNT(*)` | gross loss or duplication |
| `COUNT(DISTINCT key)` | duplication offset by loss |
| `SUM(numeric)` | substituted or corrupted values |

`COUNT(*) != COUNT(DISTINCT key)` on either side means that side is not unique
on the key. **Stop and fix that first.** Every subsequent number is meaningless
until it is resolved, and it is by far the most common finding.

## Checksum the right column

```sql
-- Wrong: floats do not sum reproducibly, and parallel aggregation varies the order
SUM(amount)

-- Right: integer minor units
SUM(amount_cents)
```

If the source stores floats, cast both sides identically and round explicitly, or
compare `COUNT` plus `MIN`/`MAX` instead and say why. A float sum that differs in
the twelfth decimal place is not a data problem, and reporting it as one wastes
everyone's morning.

## Interpreting what you find

| Symptom | Usual cause |
|---|---|
| Target has fewer rows, confined to recent days | Incremental watermark filter; late-arriving data |
| Target has fewer rows, spread evenly | A join turned inner; a filter on a nullable column |
| Target has more rows, `COUNT != COUNT(DISTINCT)` | Merge key is not the grain; a re-delivered batch |
| Counts match, sum differs | Type coercion, rounding, or a currency/unit change |
| One day missing entirely | A failed run nobody noticed |
| One day doubled | A retry of a non-idempotent load |
| Difference starts on a specific date and persists | An upstream schema or logic change on that date |
| Small constant difference | A timezone boundary, or an inclusive/exclusive range |

That last one is worth checking early: a discrepancy of exactly one day's volume,
or exactly the rows between midnight and 05:00, is a timezone problem, not a data
loss.

## `IS DISTINCT FROM`, not `<>`

When comparing values row by row:

```sql
-- Wrong: rows where either side is NULL compare as unknown and drop out
WHERE s.status <> t.status

-- Right
WHERE s.status IS DISTINCT FROM t.status
```

The mismatches you most want to find — a value that became NULL — are precisely
the ones `<>` hides.

## Across two systems

`compare_tables` works within one warehouse. Reconciling Snowflake against
Postgres, or a warehouse against a vendor export, needs both sides in one place.

Land the smaller side as a temporary table, then reconcile normally. Two things
to get right when you do:

- **Types must match.** A `VARCHAR` id on one side and an `INT` on the other will
  join to nothing and report every key as missing. Cast both to the same type
  explicitly and say which.
- **Timezones must match.** Export both in UTC, or the day buckets will not align
  and every period will look wrong.

If landing the data is not possible, compare aggregates only — daily counts and
sums by key range — and be explicit that row-level reconciliation was not done.

## Sampling, honestly

On a very large table, a full row-level comparison may be too expensive. That is
fine, but say so:

- Reconcile **aggregates in full** — they are cheap and catch most problems.
- Sample rows **deterministically** (`WHERE MOD(ABS(HASH(key)), 100) = 0`), not
  with `RANDOM()`, so the result is reproducible.
- State the sample rate and what it can and cannot rule out.

Never describe a sampled check as if it were exhaustive.

## Reporting

> `marts.fct_orders` reconciled against `raw.orders`, keyed on `order_id`.
>
> | | rows | distinct keys | SUM(amount_cents) |
> |---|---|---|---|
> | source | 2,431,908 | 2,431,908 | 184,220,411 |
> | target | 2,436,112 | 2,431,602 | 184,610,882 |
>
> **Two separate problems.**
>
> 1. **The target is not unique on `order_id`** — 4,510 duplicate rows. All of
>    them fall on 2026-03-04, which matches a re-delivered source batch. The
>    incremental merge has no watermark comparison in its `WHEN MATCHED` clause,
>    so the replay inserted rather than updated.
> 2. **306 keys are missing from the target**, spread across 2026-03-04 onward.
>    These are rows with a null `updated_at`; the incremental filter uses `>` on
>    that column, so nulls fail the comparison and are dropped on every run.
>
> Revenue is currently **overstated by 390,471 cents (~0.21%)** — the duplicates
> outweigh the missing rows.
>
> **Fix:** add the watermark comparison to the merge, switch the incremental
> filter to `_loaded_at` (pipeline-set, never null), then backfill from
> 2026-03-04. See the `incremental-backfill` skill.
>
> **Prevention:** a blocking uniqueness test on `order_id`, and a not-null test
> on the watermark column. Both would have caught this on day one.
