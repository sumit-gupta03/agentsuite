---
name: table-profiling
description: >-
  Use when first encountering a table, assessing whether data is trustworthy,
  or answering "what is in here and can I use it". Covers what to measure, what
  the numbers mean, and the sentinel values that masquerade as real data.
requires: [warehouse]
---

# Profiling a table

## Order of operations

1. `describe_table` — the schema. Never write SQL against an assumed column name.
2. `profile_table` — row count, null rates, distinct counts, numeric ranges.
3. `find_duplicates` on the presumed key — before believing anything else.
4. `check_freshness` on the load timestamp — is this table even alive?
5. `column_distribution` on anything the first four passes made suspicious.

Report findings, then conclusions. An engineer needs to see the numbers that led
you somewhere, not just the destination.

## Reading the numbers

**Null rate.** A rate of exactly 0% or exactly 100% is more interesting than
anything in between. 100% means a column the pipeline stopped populating and
nobody noticed. 0% on a nullable column often means nulls are being written as
sentinels instead.

**Distinct count vs row count.**

| Observation | What it means |
|---|---|
| `distinct = rows` | Candidate key |
| `distinct = 1` | Dead column — constant, or a default nothing overrides |
| `distinct = 2` and type is text | A boolean stored as `'Y'/'N'` or `'true'/'false'` |
| `distinct` slightly under `rows` | Duplicates. Find them before proceeding |
| `distinct` very low on a join key | Fan-out risk — this join will multiply rows |

**Numeric ranges.** Look for values that are structurally impossible rather than
merely unusual: negative quantities, ages past 150, amounts of exactly
`999999999`, dates in 1900 or 2999.

## Sentinel values

These are the ones that survive every quality check because they are technically
valid data:

- `-1`, `0`, `999`, `-999` standing in for "unknown"
- `'N/A'`, `'NULL'`, `'null'`, `'none'`, `''`, `'-'`, `'?'`, `'#N/A'`
- `1900-01-01`, `1970-01-01`, `9999-12-31`
- `'00000000-0000-0000-0000-000000000000'`
- A single repeated email or phone number — a test fixture that reached production

`column_distribution` finds these. They matter because `AVG(amount)` over a
column where unknown is `-1` returns a number that is wrong in a way no one will
question.

## Cardinality before joining

Check both sides before writing a join:

```sql
SELECT COUNT(*) AS rows, COUNT(DISTINCT join_key) AS keys FROM left_table
```

If `keys < rows` on both sides, the join is many-to-many and will produce more
rows than either input. That is occasionally intended and usually a bug. State
the expected grain of the result *before* running the join, then verify it after.

## What to report

Lead with what would break someone's work:

> `raw.orders` — 2.4M rows, loaded through 2 hours ago.
>
> - **`order_id` is not unique** — 312 keys appear twice. Any join on it inflates
>   row counts. Cause looks like a re-delivered batch on 2026-03-04.
> - `customer_email` is 34% null, and `'unknown@example.com'` accounts for a
>   further 8% — effectively 42% missing.
> - `amount` includes 1,204 rows at exactly `-1`, which is a sentinel, not a
>   refund. `AVG(amount)` is currently understated by roughly 0.4%.
> - `region` has one value (`'US'`). It was presumably meant to be populated.
>
> Usable for revenue reporting if you deduplicate on `order_id` and exclude
> `amount = -1`. Not usable for customer-level analysis at this null rate.

Not: "The table looks broadly healthy with some data quality issues."
