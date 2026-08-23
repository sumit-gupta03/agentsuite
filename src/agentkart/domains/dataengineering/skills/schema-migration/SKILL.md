---
name: schema-migration
description: >-
  Use when changing a table's schema - adding, renaming, retyping or dropping a
  column, changing a grain, or splitting a table. Covers the expand-and-contract
  sequence, which changes are safe, and how to avoid breaking downstream
  consumers you did not know existed.
requires: [warehouse]
---

# Schema migration

## Before anything: find the consumers

You cannot assess a schema change without knowing who reads the table. Check, in
this order:

1. `dbt_lineage` if the table is in a dbt project.
2. Query logs / `information_schema` view definitions for references.
3. BI tool dependencies — usually the ones nobody remembers.
4. Anything reading the table directly through an API or export.

A change that "nothing depends on" almost always has one consumer that turns up
during the incident review.

## Safety of common changes

| Change | Safe? | Notes |
|---|---|---|
| Add a nullable column | Yes | The default safe move |
| Add a column with a default | Usually | May rewrite the whole table on some engines |
| Widen a type (`int` → `bigint`) | Yes | May rewrite |
| Narrow a type | **No** | Silent truncation or failure on existing values |
| Rename a column | **No** | Breaks every consumer at once |
| Drop a column | **No** | Irreversible; do it last, weeks later |
| Change the grain | **No** | Every downstream aggregate becomes wrong |
| Change nullability to NOT NULL | Only after verifying no nulls exist | |

"No" here means *not as a single step* — use expand and contract.

## Expand and contract

The sequence that makes an unsafe change safe. A rename becomes:

1. **Expand.** Add the new column alongside the old. Both are populated by the
   pipeline. Nothing reads the new one yet.
2. **Backfill.** Populate the new column for historical rows. Verify with a
   full-table comparison, not a sample:
   ```sql
   SELECT COUNT(*) FROM t WHERE new_col IS DISTINCT FROM old_col
   ```
3. **Migrate readers.** Move consumers to the new column, one at a time, with a
   verification step for each. This phase is measured in weeks, not hours.
4. **Stop writing the old column.** Leave it in place, unpopulated, and watch for
   complaints for at least one full reporting cycle — someone runs a monthly job.
5. **Contract.** Drop the old column.

Steps 4 and 5 are where the discipline is. Skipping the soak period is how you
find the monthly consumer during month-end close.

## Type changes

Never cast in place on a live table. Add a new column, backfill with an explicit
cast, and verify the round trip:

```sql
SELECT COUNT(*) AS lossy_rows
FROM t
WHERE CAST(new_typed_col AS VARCHAR) IS DISTINCT FROM CAST(old_col AS VARCHAR)
```

Watch specifically for: string-to-numeric where the source has whitespace,
thousands separators or currency symbols; string-to-date where formats are mixed
(`03/04/2026` is ambiguous and the engine will pick one interpretation for the
whole column); and float-to-decimal, which changes stored values.

## Grain changes

The most dangerous change and the one most often done casually. Moving a table
from one row per order to one row per line item means every existing `SUM`,
`COUNT` and join against it is now wrong — and still runs.

Do not change grain in place. Create a new table at the new grain, migrate
consumers explicitly, and keep the old table until every one has moved. Naming
should make the grain obvious: `fct_orders` and `fct_order_line_items`, never
`fct_orders_v2`.

## Before executing anything

State: what changes, who is affected, how to verify success, and how to roll
back. If the rollback is "restore from backup", confirm the backup exists and
that someone has actually restored from it before.

Take a snapshot before a destructive step — `CREATE TABLE t_backup_20260823 AS
SELECT * FROM t` costs storage for a week and has saved more incidents than any
review process.
