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
