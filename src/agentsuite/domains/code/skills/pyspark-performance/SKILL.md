---
name: pyspark-performance
description: >-
  Use when a Spark job is slow, expensive, spilling, or failing with OOM, and
  when writing PySpark that has to run at scale. Covers skew, shuffle,
  partitioning, joins, caching and how to read a plan instead of guessing.
requires: [pyspark]
---

# PySpark performance

Almost every slow Spark job is slow for one of four reasons: **shuffle**,
**skew**, **partition count**, or **doing work per-row that should be per-batch**.
Diagnose which before changing anything.

## Read the plan first

```python
df.explain(mode="formatted")     # what will happen
df.explain(mode="cost")          # with size estimates, if statistics exist
```

In the physical plan, look for:

| What you see | What it means |
|---|---|
| `Exchange hashpartitioning` | a shuffle — the expensive thing |
| `SortMergeJoin` | both sides shuffled; a broadcast may be possible |
| `BroadcastHashJoin` | good — one side was small enough |
| `Filter` *above* `Scan` | predicate not pushed down; the whole file was read |
| Missing `PartitionFilters` | partition pruning did not happen |
| `CartesianProduct` | a join condition is missing. Stop and fix it |

Then read the Spark UI stage that is actually slow. **Max task duration versus
median** is the skew signal — if max is 40× median, one partition has most of the
data and adding executors will do nothing.

## Skew

The single most common cause of "one task runs for an hour".

Find it before treating it:

```python
(df.groupBy("join_key").count()
   .orderBy(F.desc("count"))
   .show(20, truncate=False))
```

A key with orders of magnitude more rows than the rest — very often `null`, `-1`,
`''`, or a default tenant id — is the culprit.

Fixes, cheapest first:

1. **Filter the sentinel out** if it is not real data. Nulls in a join key
   produce nothing on an inner join anyway; drop them explicitly and say so.
2. **Enable adaptive skew handling** (Spark 3+, usually already on):
   ```python
   spark.conf.set("spark.sql.adaptive.enabled", "true")
   spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
   ```
3. **Broadcast the small side** if it fits (see below).
4. **Salt the hot key** — last resort, because it complicates the code:
   ```python
   SALT = 16
   left  = df.withColumn("_salt", (F.rand() * SALT).cast("int"))
   right = (dim.withColumn("_salt", F.explode(F.array([F.lit(i) for i in range(SALT)]))))
   joined = left.join(right, ["join_key", "_salt"]).drop("_salt")
   ```
   Salting multiplies the small side by `SALT`. Only worth it when one key truly
   dominates and the small side is genuinely small.

## Joins

**Broadcast when one side is small.** The threshold is bytes in memory after
decompression, not file size — a 200 MB Parquet file can be 2 GB in memory.

```python
from pyspark.sql import functions as F
result = big.join(F.broadcast(small), "customer_id", "left")
```

Rules of thumb: broadcast under ~100 MB in memory; raise
`spark.sql.autoBroadcastJoinThreshold` deliberately, not reflexively. Broadcasting
something too large gives you a driver OOM, which is a worse failure than a slow
join.

**Check the join grain before you join.** If the right side is not unique on the
key, the join multiplies rows and every downstream aggregate is wrong — and it
will look like a performance problem first.

```python
assert right.groupBy("customer_id").count().filter("count > 1").isEmpty()
```

**Join order matters.** Filter both sides down before joining, not after.

## Partitions

- **Too few** — no parallelism, huge tasks, spill to disk.
- **Too many** — scheduling overhead dominates; thousands of tiny files on write.

Aim for tasks of roughly 128 MB–1 GB, and a partition count that is a small
multiple of total cores.

```python
df.rdd.getNumPartitions()

df.repartition(200, "customer_id")   # full shuffle; use to fix skew or set a key
df.coalesce(20)                      # no shuffle, only reduces; use before write
```

`coalesce` cannot increase partitions and can starve upstream parallelism if
applied too early — it changes the parallelism of the *whole* preceding stage.
Use it immediately before writing, not in the middle of a pipeline.

**On write**, partition by a low-cardinality column only:

```python
df.write.partitionBy("event_date").mode("overwrite").parquet(path)
```

Partitioning by something high-cardinality (`user_id`) creates millions of
directories and will bring down the metastore.

## Caching

Cache only when a DataFrame is used **more than once** and recomputing is
genuinely expensive. Otherwise it wastes memory and evicts things that mattered.

```python
df.cache()
df.count()          # materialise -- cache is lazy
...
df.unpersist()      # always, when done
```

An uncached `df` used five times is recomputed five times, from the source, every
time. That is often the real cost.

## Per-row work

Python UDFs serialise every row between the JVM and a Python process. They are
often 10–100× slower than the equivalent built-in.

1. **Use built-ins** — `pyspark.sql.functions` covers far more than people expect.
2. **Then pandas UDFs** — vectorised, batch-at-a-time:
   ```python
   @F.pandas_udf("double")
   def normalise(values: pd.Series) -> pd.Series:
       return (values - values.mean()) / values.std()
   ```
3. **Plain Python UDF last**, and say in a comment why nothing else worked.

Also avoid `collect()` on anything large — it pulls the whole dataset into the
driver. Use `take(n)`, `limit(n).toPandas()`, or write to storage.

## Reading and writing

- **Parquet or Delta**, never CSV, for anything intermediate.
- **Select only the columns you need**, immediately after the read. Columnar
  formats make this nearly free, and `SELECT *` throws that away.
- **Filter on the partition column** using a literal comparison so pruning
  happens: `F.col("event_date") >= "2026-01-01"`, not a function on the column.
- Check output file sizes. Hundreds of 2 MB files means the next reader will be
  slow; `coalesce` before writing.

## Before claiming an improvement

Measure. `explain()` before and after, and the actual wall clock on a
representative slice. "This should be faster" is not a result — and several of
the fixes above (salting, repartitioning, caching) make things *worse* when
applied to the wrong problem.
