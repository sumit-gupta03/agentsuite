---
name: pipeline-debugging
description: >-
  Use when a pipeline failed, produced wrong numbers, produced no rows, or "ran
  fine" but nothing appeared. Covers how to localise the fault, what to check in
  order, and the failures that leave no error behind.
---

# Debugging a pipeline

## Localise before theorising

Work upstream from the symptom, checking freshness and row counts at each layer:
mart → intermediate → staging → raw → source. The first layer that looks wrong is
where to dig; everything above it is a consequence, not a cause.

`check_freshness` at each hop is the fastest single move. A layer whose max
timestamp is older than the layer above it tells you exactly where the flow
stopped.

## The symptom table

| Symptom | Check first |
|---|---|
| No rows at all | Did the source deliver? Is the watermark filter excluding everything? |
| Fewer rows than expected | Partial load; a join turned inner; a filter on a nullable column |
| More rows than expected | Join fan-out; a re-delivered batch; a merge key that is not the grain |
| Numbers slightly off | Timezone boundary; inclusive/exclusive range; float summation |
| Numbers wildly off | Duplicated rows; wrong grain; a unit change (cents vs dollars) |
| Ran fine, no new data | Watermark did not advance; source is empty; the job read the wrong partition |
| Intermittent failure | Concurrency, a lock, or a resource limit — not the SQL |
| Fails only in production | Data volume, permissions, or a config difference. Compare the environments |

## Failures that leave no error

These are the expensive ones, because monitoring says everything is fine:

- **A nullable watermark column.** Rows with a null timestamp are dropped by the
  incremental filter every run. Forever. Nothing fails.
- **A filter on the right side of a LEFT JOIN in `WHERE`.** Silently becomes an
  inner join.
- **A schema change upstream.** A renamed column becomes null, or a `SELECT *`
  quietly picks up a new column and a downstream insert misaligns.
- **A timezone change.** Day boundaries move; one day is double-counted and one
  is empty.
- **A merge key that is not unique.** Row counts creep up over weeks.
- **A retry that ran a non-idempotent load twice.** Duplicates in exactly one
  partition.

When numbers are wrong and nothing errored, start here.

## Before declaring it fixed

1. **Reproduce the fault** on a bounded slice — you cannot verify a fix for
   something you never actually observed.
2. **Explain the mechanism.** "It works now" after a rerun means the fault is
   still there and is intermittent. Transient failures that resolve on retry are
   the ones that come back at month-end.
3. **Check whether the fault affected historical data.** A bug found today has
   usually been running for a while. Determine the blast radius and the date it
   started before deciding whether to backfill.
4. **Add the check that would have caught it.** See the `data-quality-checks`
   skill. One check per real incident is a sustainable rate.

## Reporting

State the mechanism, the blast radius, and the fix — in that order:

> `fct_orders` has been short by roughly 3% of rows since 2026-03-04.
>
> **Mechanism:** the incremental filter is `WHERE updated_at > (SELECT MAX(...))`.
> On 2026-03-04 the source began emitting rows with a null `updated_at` — about
> 3% of daily volume. Null fails the comparison, so those rows are dropped on
> every run and never picked up.
>
> **Blast radius:** 172 days, approximately 41,000 rows. Revenue totals are
> understated by roughly 2.8% for the period; customer counts are also affected.
>
> **Fix:** switch the watermark to `_loaded_at`, which the pipeline sets and
> which is never null, and add a three-day lookback. Then backfill 2026-03-04
> onward.
>
> **Prevention:** a not-null test on the watermark column, blocking.
