---
name: incremental-backfill
description: >-
  Use when backfilling an incremental table, handling late-arriving data, or
  deciding between an incremental load and a full refresh. Covers watermark
  selection, idempotency, partition-safe reruns, and the failure modes that
  only show up weeks later.
requires: [warehouse]
---

# Incremental backfill

## Decide first: incremental or full refresh?

Full refresh unless **all** of these hold:

- The source has a reliable monotonic column (see *Choosing a watermark*).
- A full rebuild is genuinely too slow or too expensive — measure it, don't assume.
- The transformation is row-independent, or window functions are partitioned in a
  way that a partial rebuild preserves.

Incremental logic is a permanent maintenance cost. A table that rebuilds in four
minutes should rebuild in four minutes.

## Choosing a watermark

Rank the candidates in this order:

1. **A warehouse-assigned load timestamp** (`_loaded_at`, `_ingested_at`). Set by
   the pipeline, monotonic by construction, immune to source clock skew.
2. **A source-side updated timestamp** (`updated_at`). Works only if the source
   updates it on *every* mutation — verify, don't trust the column name.
3. **An auto-increment surrogate key.** Catches inserts, misses updates entirely.
4. **An event timestamp** (`occurred_at`). Almost always wrong as a watermark:
   events arrive late, and you will silently drop them.

Before committing to one, run `check_freshness` on it. Null timestamps are the
tell — an incremental filter on a nullable column drops those rows on every run,
forever, and nothing alerts.

## The lookback window

Never filter on `> max(watermark)`. Use a lookback:

```sql
WHERE loaded_at >= (SELECT COALESCE(MAX(loaded_at), '1900-01-01') FROM {{ target }})
                   - INTERVAL '3 days'
```

Sizing the window: measure the actual arrival lag distribution first.

```sql
SELECT
  DATE_DIFF('hour', occurred_at, loaded_at) AS lag_hours,
  COUNT(*)                                  AS n
FROM source
GROUP BY 1
ORDER BY 1 DESC
LIMIT 50
```

Set the lookback past the p99.9, not the p50. The tail is the whole point. Then
re-measure quarterly — upstream changes and nobody tells you.

A lookback only works if the load is idempotent, which brings us to:

## Idempotency

The load must produce the same result whether it runs once, twice, or five times
after a retry storm. In practice that means a merge keyed on the grain:

```sql
MERGE INTO target t
USING staged s ON t.order_id = s.order_id
WHEN MATCHED AND s.loaded_at > t.loaded_at THEN UPDATE SET ...
WHEN NOT MATCHED THEN INSERT ...
```

Two conditions people get wrong:

- **The merge key must be the actual grain.** Verify with `find_duplicates` on
  the staged data *before* the first merge, not after the row counts look odd.
- **The `WHEN MATCHED` guard must compare watermarks**, or a replayed older batch
  overwrites newer data with stale values. This corrupts silently and is
  extremely hard to detect later.

If the warehouse has no MERGE, use delete-then-insert inside one transaction,
scoped to the affected partitions. Never delete outside a transaction.

## Running the backfill

1. **Bound it.** One partition, or one narrow date range, into a scratch table.
2. **Reconcile against the source** before going wider — row counts by day, and
   a sum of one meaningful numeric column. Counts alone hide duplicated rows
   offset by dropped ones.
3. **Chunk by partition, oldest first**, so a failure halfway leaves a coherent
   prefix rather than a hole.
4. **Record what you backfilled** — range, run time, row counts — somewhere
   durable. The next person to ask "was March reloaded?" will be you.

Never issue an unbounded `DELETE` on the target as step one of a backfill. If the
reload then fails, you have destroyed data you cannot reconstruct.

## Failure modes to check for explicitly

| Symptom | Usual cause |
|---|---|
| Row counts drift up over time | Merge key is not the grain; duplicates accumulate |
| Recent data missing, older data fine | Watermark filter is `>` with no lookback |
| Rows silently dropped every run | Nullable watermark column |
| Older values overwrite newer | Merge lacks a watermark comparison |
| A backfilled range is short by exactly one day | Inclusive/exclusive boundary error, or a timezone mismatch |
| Full refresh differs from incremental | The transformation is not row-independent |

The last one is the audit worth building: periodically rebuild into a scratch
table and diff against the incremental target. Drift means the incremental logic
has a bug, and it has been wrong for as long as it has been running.

See `reference/reconciliation.md` for the diff queries.
