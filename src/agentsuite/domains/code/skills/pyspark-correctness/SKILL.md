---
name: pyspark-correctness
description: >-
  Use when writing or reviewing PySpark transformations. Covers the traps that
  produce a plausible wrong number rather than an error - null semantics,
  non-deterministic operations, window frames, schema drift and silent type
  coercion.
requires: [pyspark]
---

# PySpark correctness

Spark rarely errors on bad logic. It returns a DataFrame with the right column
names and the wrong numbers, and nobody notices for a quarter.

## Nulls

Spark uses three-valued logic. `NULL = NULL` is `NULL`, not `TRUE`.

```python
df.filter(F.col("status") != "cancelled")   # DROPS rows where status is null
df.filter((F.col("status") != "cancelled") | F.col("status").isNull())  # right
```

This is the single most common silent data loss in Spark. Any `!=` or `NOT IN`
filter on a nullable column quietly excludes the nulls.

Other null behaviour worth knowing:

- **Aggregates skip nulls.** `avg` over `[1, 2, NULL]` is `1.5`, not `1.0`.
  `count(col)` skips them; `count("*")` does not.
- **Joins drop them.** `NULL` never matches `NULL` on an equi-join. If you need
  them matched, use `<=>` (`eqNullSafe`).
- **Concatenation poisons.** `concat("a", NULL)` is `NULL`. Use `concat_ws`.
- **`sum` of an all-null column is `NULL`, not 0.** Wrap in `coalesce` when zero
  is meant.

Check the null rate of any column you are about to filter or join on, before you
write the logic.

## Joins

**Establish the grain of both sides before joining, and verify the output grain
after.** A join to a non-unique key multiplies rows.

```python
before = orders.count()
joined = orders.join(customers, "customer_id", "left")
assert joined.count() == before, "left join changed row count -- customers is not unique"
```

A `left` join that changes the row count means the right side has duplicate keys.
That is a bug in the data or in your understanding, never something to paper over
with `dropDuplicates`.

**Filtering the right side after a left join turns it into an inner join:**

```python
# Wrong: drops orders that had no matching customer
orders.join(customers, "customer_id", "left").filter(F.col("region") == "EU")

# Right
orders.join(customers.filter(F.col("region") == "EU"), "customer_id", "left")
```

**Ambiguous columns.** After joining two DataFrames with the same column name,
`df["col"]` may be ambiguous or silently pick one. Rename before joining, or use
the `df.col` / `other.col` forms explicitly.

## Non-determinism

These produce different results on retry, which breaks idempotency and makes
failures unreproducible:

- `monotonically_increasing_id()` — depends on partitioning. Not a stable key,
  not contiguous, and changes between runs. Never persist it as an id.
- `collect_list` / `collect_set` — order is not guaranteed. Sort explicitly if
  order matters.
- `first` / `last` without an `orderBy` — arbitrary.
- `rand()` without a seed.
- `dropDuplicates` without `orderBy` — *which* duplicate survives is arbitrary.
  Use a window with an explicit, total ordering:

```python
window = Window.partitionBy("order_id").orderBy(F.col("loaded_at").desc(), F.col("source").asc())
deduped = (df.withColumn("_rn", F.row_number().over(window))
             .filter(F.col("_rn") == 1)
             .drop("_rn"))
```

Note the tiebreaker. An ordering that is not total is still non-deterministic.

## Window frames

The default frame for an ordered window is `RANGE BETWEEN UNBOUNDED PRECEDING AND
CURRENT ROW`, which includes **peer rows** — every row with an equal ordering
value. For a true running total, say `rowsBetween`:

```python
running = Window.partitionBy("account").orderBy("ts").rowsBetween(Window.unboundedPreceding, 0)
```

A window with `orderBy` and no explicit frame, over data with ties, silently
double-counts. This is subtle and common.

Also: a window without `partitionBy` moves the entire dataset into one partition.
It works on a sample and falls over in production.

## Types and schema

- **`inferSchema` is a guess.** It samples, gets `"00123"` wrong, turns
  `"2026-01-02"` into a string or a date depending on the file, and changes
  between runs when the data changes. Declare an explicit `StructType` for
  anything that matters.
- **Decimal for money.** Doubles do not sum reproducibly, and Spark's
  parallel aggregation makes the order vary between runs.
- **Silent casts.** `col.cast("int")` on `"abc"` gives `NULL`, not an error. After
  any cast, count the nulls it produced:
  ```python
  df.filter(F.col("amount").isNotNull() & F.col("amount_int").isNull()).count()
  ```
- **`SELECT *` with an evolving upstream** breaks positional inserts and picks up
  columns you did not intend. Name columns explicitly outside of staging.

## Timestamps

`spark.sql.session.timeZone` affects how timestamps are parsed and displayed.
Two people running the same job in different sessions get different day
boundaries. Set it explicitly in the job, store UTC, and cast with an explicit
zone.

## Verify

Cheap checks that catch most of the above, worth adding to any job that matters:

```python
assert df.count() > 0, "empty result"
assert df.groupBy(*keys).count().filter("count > 1").isEmpty(), "grain is not unique"
assert df.filter(F.col(watermark).isNull()).count() == 0, "null watermark"
```

Run them on a bounded slice first. A correctness check that is too slow to run
never runs.
