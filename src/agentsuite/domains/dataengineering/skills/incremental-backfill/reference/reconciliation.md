# Reconciliation queries

Run these after any backfill, and on a schedule for tables that matter.

## 1. Row counts by day

The cheapest check, and it catches gross errors.

```sql
SELECT
  COALESCE(s.d, t.d)                         AS day,
  s.n                                        AS source_rows,
  t.n                                        AS target_rows,
  COALESCE(t.n, 0) - COALESCE(s.n, 0)        AS delta
FROM (
  SELECT CAST(loaded_at AS DATE) AS d, COUNT(*) AS n
  FROM source WHERE loaded_at >= :start AND loaded_at < :end GROUP BY 1
) s
FULL OUTER JOIN (
  SELECT CAST(loaded_at AS DATE) AS d, COUNT(*) AS n
  FROM target WHERE loaded_at >= :start AND loaded_at < :end GROUP BY 1
) t ON s.d = t.d
WHERE COALESCE(s.n, 0) <> COALESCE(t.n, 0)
ORDER BY day
```

Empty result means counts agree. Counts agreeing does **not** mean the data
agrees — duplicated rows offset by dropped rows produce an identical count.

## 2. Checksum by day

Catches what counts miss. Sum a numeric column that would change if rows were
substituted, and count distinct keys.

```sql
SELECT
  CAST(loaded_at AS DATE)   AS day,
  COUNT(*)                  AS n,
  COUNT(DISTINCT order_id)  AS distinct_keys,
  SUM(amount_cents)         AS total_cents
FROM target
WHERE loaded_at >= :start AND loaded_at < :end
GROUP BY 1
ORDER BY 1
```

`n <> distinct_keys` means the merge key is not the grain. Stop and fix that
before anything else — every downstream number is wrong.

Sum a **cents integer**, not a float. Floating-point sums are order-dependent and
will differ between source and target for reasons that have nothing to do with
your pipeline.

## 3. Full-refresh drift audit

The check that finds bugs which have been running for months.

```sql
-- Rebuild the full logic into a scratch table, then:
SELECT 'missing_in_target' AS issue, COUNT(*) AS n
FROM scratch_full s
LEFT JOIN target t USING (order_id)
WHERE t.order_id IS NULL

UNION ALL

SELECT 'extra_in_target', COUNT(*)
FROM target t
LEFT JOIN scratch_full s USING (order_id)
WHERE s.order_id IS NULL

UNION ALL

SELECT 'value_mismatch', COUNT(*)
FROM target t
JOIN scratch_full s USING (order_id)
WHERE t.amount_cents IS DISTINCT FROM s.amount_cents
   OR t.status       IS DISTINCT FROM s.status
```

Use `IS DISTINCT FROM`, not `<>`. With `<>`, a row where one side is NULL
compares as unknown and drops out of the result — the mismatches you most want
to find are exactly the ones it hides.

## 4. Late-arrival monitor

Run weekly. When the tail moves past your lookback window, the window is now too
small and you are dropping data.

```sql
SELECT
  DATE_DIFF('hour', occurred_at, loaded_at) AS lag_hours,
  COUNT(*)                                  AS n
FROM source
WHERE loaded_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1
ORDER BY lag_hours DESC
LIMIT 20
```
