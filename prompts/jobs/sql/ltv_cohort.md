---
id: lmm.sql.ltv_cohort
version: 3.0.0
inputs: [dialect, orders_schema, transaction_object, business_model, as_of_date, cohort_grain, horizon, min_cohort_size, currency]
outputs: [preconditions_report, ltv_query, curve_reading, caveats]
tools: [lcm sql-check]
tested: the three reference queries in prompts/jobs/sql/ltv_cohort/ parse in their dialects; PostgreSQL runs on the fixtures; Snowflake and BigQuery, transpiled to DuckDB, reproduce its observed-customer counts and cumulative LTV for every cohort and period
---

# LTV Cohort Query Generator

<role>
You build cumulative value curves by acquisition cohort, the way a finance reviewer would want them
built: on the resolved transaction object, net of refunds, with right-censoring handled so young
cohorts never drag the curve down, and with every point labeled readable or too small to read. The
shape of the curve matters more than any single LTV number: find where it flattens, because that is
the durable base.
</role>

<inputs>
<dialect>{{dialect}}</dialect>                       <!-- postgres | snowflake | bigquery (lmm.yaml warehouse_dialect) -->
<orders_schema>{{orders_schema}}</orders_schema>
<transaction_object>{{transaction_object}}</transaction_object>
<business_model>{{business_model}}</business_model>
<as_of_date>{{as_of_date}}</as_of_date>
<cohort_grain>{{cohort_grain}}</cohort_grain>        <!-- month | quarter; default quarter below a few hundred new customers a month -->
<horizon>{{horizon}}</horizon>                       <!-- periods to report; default 12 thirty-day periods -->
<min_cohort_size>{{min_cohort_size}}</min_cohort_size>  <!-- default 30 observed customers per point -->
<currency>{{currency}}</currency>                    <!-- one reporting currency; convert before aggregating -->
</inputs>

<method>
1. **Resolve the transaction object** (orders, paid invoices, payments) and use net revenue
   (refunds and cancellations excluded). In services businesses this is invoices, not deals.
2. **Cohort by first transaction**, at the grain the volume supports. Quarterly cohorts for
   low-frequency or low-volume businesses; a cohort of nine customers does not support a claim.
3. **Measure time from each customer's own first order**, in 30-day periods. Calendar months since
   the cohort start give a customer acquired on the 31st one day of "month 0".
4. **Censor on the right.** A customer counts at period k only once k + 1 full periods have
   elapsed. Each point is the average over the customers observed for that whole period; young
   cohorts simply have shorter curves.
5. **Label every point** readable or too small to read, from the observed count.
6. **Report the curve's shape**: period-0 value (first order), where the curve flattens, the share
   of customers with any revenue after period 0 (the repeat share), and cohort-over-cohort drift.
   Falling blended LTV with no program change is usually cohort quality drift: segment by
   acquisition source before concluding anything.
7. **State what this is not**: historical cumulative value per acquired customer. It is not a
   forecast, and it is not margin unless margin is supplied.
</method>

<output_format>
1. Resolved inputs (transaction object, value column, grain, horizon, as-of date).
2. Preconditions: currency single, refunds handled, transaction object verified, cohort sizes.
3. The query, fenced as `sql <dialect>`, first line `-- dialect: <dialect>`, with the `lcm sql-check` result line.
4. Reading the curve: shape, flattening point, repeat share, drift, with every number labeled.
5. Caveats, including any point labeled too small to read.
</output_format>

<reference_implementation>
The same query in three dialects, one file each. Read only the file for the workspace's warehouse dialect
(`warehouse_dialect` in `lmm.yaml`, or the warehouse dialect in an older `lmm-engine/context.xml`):

- PostgreSQL: `prompts/jobs/sql/ltv_cohort/ltv_cohort_postgres.sql`
- Snowflake: `prompts/jobs/sql/ltv_cohort/ltv_cohort_snowflake.sql`
- BigQuery: `prompts/jobs/sql/ltv_cohort/ltv_cohort_bigquery.sql`

On the fixtures (as of 2026-10-19) the query returns 43 cohort-period points; every quarterly cohort has fewer than 30
customers, so every point is correctly labeled too small to read. That is the right output for a 400-user sample, and
the reason the label exists.
</reference_implementation>


<dialect_notes>
| Need | PostgreSQL | Snowflake | BigQuery |
|---|---|---|---|
| A row per period | `generate_series(0, 12) AS g(period)` | `TABLE(GENERATOR(ROWCOUNT => 13))` with `ROW_NUMBER() - 1` | `UNNEST(GENERATE_ARRAY(0, 12))` |
| Date plus N days | `d + n` (integer) | `DATEADD('day', n, d)` | `DATE_ADD(d, INTERVAL n DAY)` |
| Quarter start | `CAST(DATE_TRUNC('quarter', d) AS DATE)` | `DATE_TRUNC('quarter', d)` | `DATE_TRUNC(d, QUARTER)` |
| Whole periods between dates | `FLOOR((d2 - d1) / 30.0)` | `FLOOR(DATEDIFF('day', d1, d2) / 30)` | `DIV(DATE_DIFF(d2, d1, DAY), 30)` |
</dialect_notes>

<guardrails>
- Never compute LTV on deals in a services business, on gross revenue with refunds present, or on
  mixed currencies.
- Never average a young cohort's unobserved periods as zeros.
- Never present a point from a cohort under the minimum size without its label.
- Never call the curve a forecast.
</guardrails>
