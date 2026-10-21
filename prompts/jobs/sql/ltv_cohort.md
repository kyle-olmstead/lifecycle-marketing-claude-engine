---
id: lmm.sql.ltv_cohort_query_generator
version: 2.0.0
module: 04_data_schemas_and_sql
persona: Senior CRM Systems and Data Engineer, with the Principal Lifecycle Strategist interpreting the curves
inputs: [dialect, orders_schema, transaction_object, business_model, as_of_date, cohort_grain, horizon, min_cohort_size, currency]
outputs: [preconditions_report, ltv_query, curve_reading, caveats]
tools: [engine/sql/check.py]
tested: the three reference queries parse in their dialects; PostgreSQL runs on the fixtures; Snowflake and BigQuery, transpiled to DuckDB, reproduce its observed-customer counts and cumulative LTV for every cohort and period
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
<dialect>{{dialect}}</dialect>                       <!-- snowflake | bigquery | postgresql -->
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
3. The query, fenced as `sql <dialect>`, first line `-- dialect: <dialect>`.
4. Reading the curve: shape, flattening point, repeat share, drift, with every number labeled.
5. Caveats, including any point labeled too small to read.
</output_format>

<reference_implementation>
On the fixtures (as of 2026-10-19) the query returns 43 cohort-period points; every quarterly cohort
has fewer than 30 customers, so every point is correctly labeled too small to read. That is the
right output for a 400-user sample, and the reason the label exists.

### PostgreSQL

```sql postgres
-- dialect: postgresql
-- transaction object: orders, status = 'completed', net_amount > 0
-- cumulative net revenue per customer by 30-day period since each customer's own first order,
-- averaged within first-order-quarter cohorts, counting only customers observed for the full period
WITH params AS (
  SELECT
    DATE '2026-10-19' AS as_of_date,
    12                AS max_period,        -- periods 0 to 12 (about the first year)
    30                AS min_customers      -- below this many observed customers a point is labeled too small to read
),

orders_clean AS (
  SELECT o.user_id, CAST(o.order_at AS DATE) AS order_date, o.net_amount
  FROM orders o
  CROSS JOIN params p
  WHERE o.status = 'completed' AND o.net_amount > 0 AND CAST(o.order_at AS DATE) <= p.as_of_date
),

customers AS (
  SELECT
    user_id,
    MIN(order_date)                                             AS first_order_date,
    CAST(DATE_TRUNC('quarter', MIN(order_date)) AS DATE)        AS cohort_quarter
  FROM orders_clean
  GROUP BY user_id
),

periods AS (
  SELECT g.period FROM generate_series(0, 12) AS g(period)
),

-- right-censoring: a customer counts at period k only once k + 1 full 30-day periods have elapsed
observed AS (
  SELECT c.user_id, c.cohort_quarter, pr.period
  FROM customers c
  CROSS JOIN periods pr
  CROSS JOIN params p
  WHERE pr.period <= p.max_period
    AND c.first_order_date + CAST((pr.period + 1) * 30 AS INTEGER) <= p.as_of_date
),

revenue_by_period AS (
  SELECT o.user_id, FLOOR((o.order_date - c.first_order_date) / 30.0) AS period, SUM(o.net_amount) AS revenue
  FROM orders_clean o
  JOIN customers c ON c.user_id = o.user_id
  GROUP BY o.user_id, FLOOR((o.order_date - c.first_order_date) / 30.0)
),

cumulative AS (
  SELECT
    ob.cohort_quarter, ob.user_id, ob.period,
    SUM(COALESCE(r.revenue, 0)) OVER (PARTITION BY ob.user_id ORDER BY ob.period
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue,
    COALESCE(r0.revenue, 0)                                       AS period0_revenue   -- the first order's period
  FROM observed ob
  LEFT JOIN revenue_by_period r ON r.user_id = ob.user_id AND r.period = ob.period
  LEFT JOIN revenue_by_period r0 ON r0.user_id = ob.user_id AND r0.period = 0
)

SELECT
  cu.cohort_quarter,
  cu.period                                                                  AS period_30d,
  COUNT(*)                                                                   AS customers_observed,
  ROUND(AVG(cu.cumulative_revenue), 2)                                       AS cumulative_ltv_per_customer,
  -- every customer has revenue in period 0 (that is the first order), so the readable share is revenue after it
  ROUND(AVG(CASE WHEN cu.cumulative_revenue > cu.period0_revenue THEN 1.0 ELSE 0 END), 3) AS repeat_share,
  CASE WHEN COUNT(*) >= MAX(p.min_customers) THEN 'readable' ELSE 'too small to read' END AS read
FROM cumulative cu
CROSS JOIN params p
GROUP BY cu.cohort_quarter, cu.period
ORDER BY cu.cohort_quarter, cu.period;
```

### Snowflake

```sql snowflake
-- dialect: snowflake
-- transaction object: orders, status = 'completed', net_amount > 0
WITH params AS (
  SELECT DATE '2026-10-19' AS as_of_date, 12 AS max_period, 30 AS min_customers
),

orders_clean AS (
  SELECT o.user_id, TO_DATE(o.order_at) AS order_date, o.net_amount
  FROM orders o
  CROSS JOIN params p
  WHERE o.status = 'completed' AND o.net_amount > 0 AND TO_DATE(o.order_at) <= p.as_of_date
),

customers AS (
  SELECT user_id, MIN(order_date) AS first_order_date, DATE_TRUNC('quarter', MIN(order_date)) AS cohort_quarter
  FROM orders_clean
  GROUP BY user_id
),

periods AS (
  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS period
  FROM TABLE(GENERATOR(ROWCOUNT => 13))
),

observed AS (
  SELECT c.user_id, c.cohort_quarter, pr.period
  FROM customers c
  CROSS JOIN periods pr
  CROSS JOIN params p
  WHERE pr.period <= p.max_period
    AND DATEADD('day', (pr.period + 1) * 30, c.first_order_date) <= p.as_of_date
),

revenue_by_period AS (
  SELECT o.user_id, FLOOR(DATEDIFF('day', c.first_order_date, o.order_date) / 30) AS period, SUM(o.net_amount) AS revenue
  FROM orders_clean o
  JOIN customers c ON c.user_id = o.user_id
  GROUP BY o.user_id, FLOOR(DATEDIFF('day', c.first_order_date, o.order_date) / 30)
),

cumulative AS (
  SELECT
    ob.cohort_quarter, ob.user_id, ob.period,
    SUM(COALESCE(r.revenue, 0)) OVER (PARTITION BY ob.user_id ORDER BY ob.period
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue,
    COALESCE(r0.revenue, 0)                                       AS period0_revenue   -- the first order's period
  FROM observed ob
  LEFT JOIN revenue_by_period r ON r.user_id = ob.user_id AND r.period = ob.period
  LEFT JOIN revenue_by_period r0 ON r0.user_id = ob.user_id AND r0.period = 0
)

SELECT
  cu.cohort_quarter,
  cu.period                                                        AS period_30d,
  COUNT(*)                                                         AS customers_observed,
  ROUND(AVG(cu.cumulative_revenue), 2)                             AS cumulative_ltv_per_customer,
  ROUND(AVG(IFF(cu.cumulative_revenue > cu.period0_revenue, 1.0, 0)), 3) AS repeat_share,
  IFF(COUNT(*) >= MAX(p.min_customers), 'readable', 'too small to read') AS read
FROM cumulative cu
CROSS JOIN params p
GROUP BY cu.cohort_quarter, cu.period
ORDER BY cu.cohort_quarter, cu.period;
```

### BigQuery

```sql bigquery
-- dialect: bigquery
-- transaction object: orders, status = 'completed', net_amount > 0
-- qualify table names with your dataset
WITH params AS (
  SELECT DATE '2026-10-19' AS as_of_date, 12 AS max_period, 30 AS min_customers
),

orders_clean AS (
  SELECT o.user_id, DATE(o.order_at) AS order_date, o.net_amount
  FROM orders AS o
  CROSS JOIN params AS p
  WHERE o.status = 'completed' AND o.net_amount > 0 AND DATE(o.order_at) <= p.as_of_date
),

customers AS (
  SELECT user_id, MIN(order_date) AS first_order_date, DATE_TRUNC(MIN(order_date), QUARTER) AS cohort_quarter
  FROM orders_clean
  GROUP BY user_id
),

periods AS (
  SELECT period FROM UNNEST(GENERATE_ARRAY(0, 12)) AS period
),

observed AS (
  SELECT c.user_id, c.cohort_quarter, pr.period
  FROM customers AS c
  CROSS JOIN periods AS pr
  CROSS JOIN params AS p
  WHERE pr.period <= p.max_period
    AND DATE_ADD(c.first_order_date, INTERVAL (pr.period + 1) * 30 DAY) <= p.as_of_date
),

revenue_by_period AS (
  SELECT o.user_id, DIV(DATE_DIFF(o.order_date, c.first_order_date, DAY), 30) AS period, SUM(o.net_amount) AS revenue
  FROM orders_clean AS o
  JOIN customers AS c ON c.user_id = o.user_id
  GROUP BY o.user_id, period
),

cumulative AS (
  SELECT
    ob.cohort_quarter, ob.user_id, ob.period,
    SUM(IFNULL(r.revenue, 0)) OVER (PARTITION BY ob.user_id ORDER BY ob.period
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue,
    IFNULL(r0.revenue, 0)                                       AS period0_revenue   -- the first order's period
  FROM observed AS ob
  LEFT JOIN revenue_by_period AS r ON r.user_id = ob.user_id AND r.period = ob.period
  LEFT JOIN revenue_by_period AS r0 ON r0.user_id = ob.user_id AND r0.period = 0
)

SELECT
  cu.cohort_quarter,
  cu.period                                                     AS period_30d,
  COUNT(*)                                                      AS customers_observed,
  ROUND(AVG(cu.cumulative_revenue), 2)                          AS cumulative_ltv_per_customer,
  ROUND(AVG(IF(cu.cumulative_revenue > cu.period0_revenue, 1.0, 0)), 3) AS repeat_share,
  IF(COUNT(*) >= ANY_VALUE(p.min_customers), 'readable', 'too small to read') AS read
FROM cumulative AS cu
CROSS JOIN params AS p
GROUP BY cu.cohort_quarter, cu.period
ORDER BY cu.cohort_quarter, cu.period;
```
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
