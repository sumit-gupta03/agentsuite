---
name: dbt-model-authoring
description: >-
  Use when writing or modifying a dbt model, choosing a materialisation, or
  structuring a dbt project. Covers layer conventions, incremental
  configuration, and the checks to run before materialising anything.
requires: [dbt]
---

# Authoring dbt models

## Always compile before you run

`dbt_compile` then `dbt_show_compiled` shows the SQL that will actually execute
after Jinja, macros and incremental predicates resolve. Reading the `.sql` source
tells you what was written, not what will run — and incremental models in
particular compile to something quite different on the first run than on the
second.

Run `dbt_lineage` before changing an existing model, so you know what breaks.

## Layers

- **staging** — one model per source table. Rename, cast, and nothing else. No
  joins, no business logic. Materialised as views. Named `stg_<source>__<entity>`.
- **intermediate** — joins and reshaping that more than one mart needs. Not
  exposed to consumers. Named `int_<entity>__<verb>`.
- **marts** — business-facing. Tables. Named `fct_<event>` or `dim_<entity>`.

The value of the convention is that the grain is predictable from the name. Keep
it even when a shortcut is tempting; the shortcut is what makes lineage
unreadable two years in.

## Materialisation

Default to `view` for staging and `table` for marts. Reach for `incremental` only
when a full rebuild is measurably too slow or too expensive — measure first.

`ephemeral` is a trap for anything non-trivial: it inlines into every consumer,
so a single change re-runs everywhere, and it cannot be inspected in the
warehouse when something goes wrong.

## Incremental models

```sql
{{ config(
    materialized='incremental',
    unique_key='order_id',
    incremental_strategy='merge',
    on_schema_change='append_new_columns'
) }}

SELECT ... FROM {{ ref('stg_orders') }}

{% if is_incremental() %}
WHERE loaded_at >= (SELECT MAX(loaded_at) FROM {{ this }}) - INTERVAL '3 days'
{% endif %}
```

Four things to verify every time:

- **`unique_key` is genuinely unique** in the source. Run `find_duplicates`
  against `stg_orders` before trusting it. dbt does not check for you, and a
  non-unique merge key duplicates rows silently.
- **The lookback window exists.** A bare `> MAX(...)` drops late-arriving rows.
  See the `incremental-backfill` skill.
- **`on_schema_change`** is set deliberately. The default (`ignore`) means a new
  upstream column is silently absent from your model.
- **The filter is inside `is_incremental()`.** Outside it, the first full build
  is also filtered, and the model is permanently missing history.

## Tests

Put the four load-bearing tests on every mart: `unique` and `not_null` on the
grain, `not_null` on join keys, and a freshness or recency assertion. Add
`relationships` only where the referenced table loads first.

Resist testing `accepted_values` on a field the source system owns — it will add
a value and fail your pipeline over something that is fine.

## Before materialising

1. `dbt_list` with the selector, to see exactly what will be rebuilt. `+`
   operators fan out further than people expect.
2. `dbt_compile` and read the SQL.
3. `dbt_test` on the upstream models — build on top of data you have verified.
4. Only then `dbt_run`, and only with the narrowest selector that does the job.

`--full-refresh` on a large incremental model can cost hours and real money.
Never pass it as a way of "making sure"; pass it when you understand why the
incremental state is wrong.
