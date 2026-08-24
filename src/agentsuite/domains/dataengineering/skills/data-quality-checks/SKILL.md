---
name: data-quality-checks
description: >-
  Use when asked to add data quality tests, set up monitoring, or decide what to
  assert about a table. Covers which checks earn their place, how to set
  thresholds that do not page people at 3am for nothing, and what to do on failure.
requires: [warehouse]
---

# Data quality checks

## The four that always earn their place

Everything else is situational; these are not.

1. **Uniqueness of the grain.** If the primary key is not unique, every number
   downstream is wrong. This is the check that catches the most damage.
2. **Not-null on the join keys and the grain.** A null key silently drops rows
   from every downstream join.
3. **Freshness.** A pipeline that stopped running looks identical to a pipeline
   with no new data. Assert on max load timestamp, not on row count.
4. **Row count within an expected band.** Catches partial loads, which are far
   more common than total failures and far harder to notice.

## Checks that are usually noise

- Not-null on every column. You will exempt half of them within a month, and the
  exemption list becomes the real schema.
- Accepted-values on a field the source owns. It will add a value, you will not
  be told, and the pipeline will fail at 3am over a new status code that is fine.
- Exact row-count equality between environments.
- Referential integrity to a dimension that loads *after* the fact table. This
  fails on ordering, not on data.

A check nobody acts on is worse than no check: it trains the team to ignore
alerts, and the one real failure gets ignored too.

## Setting thresholds

Measure before asserting. Query the last 90 days, look at the actual
distribution, and set the bound outside the observed range with headroom:

```sql
SELECT
  CAST(loaded_at AS DATE) AS day,
  COUNT(*)                AS n
FROM orders
WHERE loaded_at >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY 1
ORDER BY 1
```

Then account for the shape of the data, not just its range:

- **Weekly seasonality** — compare Monday to Mondays, not to Sunday.
- **Growth trend** — a fixed lower bound set in January fails every day by June.
  Use a rolling comparison: within 40% of the trailing 28-day median for the same
  weekday.
- **Known outliers** — Black Friday, month-end batch, the annual migration.

## Severity

Split checks into two tiers, and mean it:

**Blocking** — stop the pipeline, do not publish. Reserve for checks where
serving the data does more harm than serving nothing: duplicate grain, null keys,
row count near zero.

**Warning** — publish, record, review in the morning. Everything else.

Every check needs an owner and a documented response. "Alert fires, someone looks
into it" is not a response; it is how alerts get muted.

## Where to check

Test at the boundary you control:

- **On ingestion** — schema conformance, parse failures, file completeness.
- **On staging** — grain uniqueness, not-null keys, type coercion failures.
- **On marts** — business invariants (revenue is non-negative, statuses are in a
  known set, totals reconcile to the source system).

Testing the same assertion at three layers is duplicated maintenance and
triplicate alerts for one incident.

## Reconciliation is the check people skip

The highest-value check is usually comparing your aggregate to the source system
of record:

```sql
SELECT
  CAST(order_date AS DATE) AS day,
  SUM(amount_cents)        AS warehouse_total
FROM marts.fct_orders
WHERE order_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1
```

against the same figure from the operational database or the finance export. A
persistent discrepancy under 1% is usually a timezone or a cutoff boundary. A
discrepancy that grows is usually duplicate rows.
