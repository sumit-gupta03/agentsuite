---
name: sql-review
description: >-
  Use when reviewing, debugging or writing non-trivial SQL - joins, window
  functions, aggregations, CTEs. Covers correctness traps that return a plausible
  wrong number rather than an error, plus performance patterns worth flagging.
---

# Reviewing SQL

Bad SQL rarely errors. It returns a number that looks reasonable and is wrong.
Review for correctness first; performance is a distant second.

## Correctness traps, most common first

**NULL in `NOT IN`.** If the subquery returns a single NULL, `NOT IN` returns no
rows. Silently. Use `NOT EXISTS`.

```sql
-- returns nothing if any customer_id is NULL
WHERE id NOT IN (SELECT customer_id FROM orders)
-- correct
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = t.id)
```

**A filter on the right table of a LEFT JOIN.** Putting the condition in `WHERE`
silently converts the join to an INNER JOIN. It belongs in `ON`.

```sql
LEFT JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'complete'          -- drops customers with no orders
LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'complete'   -- correct
```

**Join fan-out.** Joining to a table where the key is not unique multiplies rows,
and every downstream `SUM` is then inflated. Establish the grain of each input
before joining, and state the expected grain of the output.

**`COUNT(column)` vs `COUNT(*)`.** `COUNT(column)` skips NULLs. Usually a bug
when the intent was "how many rows".

**Aggregates over an outer join.** `SUM` returns NULL for a group with no
matching rows, `COUNT` returns 0. Wrap in `COALESCE` when zero is meant.

**Integer division.** In Postgres and most engines `1/2` is `0`. Cast before
dividing, and guard the denominator with `NULLIF(x, 0)`.

**Float money.** Sums of floats are order-dependent and will not reconcile.
Store and sum integer minor units.

**Timezones.** `CAST(ts AS DATE)` uses the session timezone. Two people running
the same query in different sessions get different day boundaries. Be explicit:
`CAST(ts AT TIME ZONE 'UTC' AS DATE)`.

**`BETWEEN` on timestamps.** `BETWEEN '2026-01-01' AND '2026-01-31'` excludes
almost all of 31 January, because the upper bound is midnight. Use half-open
ranges: `>= '2026-01-01' AND < '2026-02-01'`.

**`DISTINCT` covering a bug.** A `SELECT DISTINCT` added to make row counts look
right is nearly always hiding a fan-out. Find the join that duplicated the rows.

**Window frames.** The default frame for an ordered window is `RANGE BETWEEN
UNBOUNDED PRECEDING AND CURRENT ROW`, which includes *peer rows* with equal
ordering values. For a true running total use `ROWS BETWEEN`.

**`QUALIFY` / dedup by `ROW_NUMBER`.** Verify the `PARTITION BY` is the intended
grain and the `ORDER BY` is deterministic. A tie broken arbitrarily makes the
pipeline non-reproducible.

## Performance, in order of impact

1. **Partition and cluster keys.** A filter that does not touch the partition
   column scans everything. Check the plan, not the intent.
2. **Functions on filtered columns.** `WHERE DATE(ts) = '2026-01-01'` cannot use
   an index or prune partitions. Rewrite as a range.
3. **`SELECT *` in a columnar warehouse.** You are paying for every column.
4. **Filter before joining**, not after — push predicates into subqueries or CTEs.
5. **CTE materialisation.** Some engines inline CTEs, some materialise them. A
   CTE referenced three times may execute three times. Check the plan.
6. **`DISTINCT` and `ORDER BY` on large sets** force a sort or shuffle. Often
   avoidable.

Always `explain_query` before claiming something is slow or fast.

## Reviewing output

Be specific and rank by consequence:

> Two correctness issues:
>
> 1. Line 14 — `WHERE o.status = 'complete'` on a LEFT JOIN turns it into an
>    INNER JOIN. Customers with no completed orders vanish. Move it into `ON`.
> 2. Line 22 — `SUM(li.amount)` after joining `line_items` at item grain while
>    the query is at order grain. Every order with multiple items is
>    double-counted. Aggregate line items in a subquery first.
>
> Performance: line 8 filters `DATE(created_at) = ...`, which prevents partition
> pruning. Rewrite as `created_at >= '...' AND created_at < '...'`.
