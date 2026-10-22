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
