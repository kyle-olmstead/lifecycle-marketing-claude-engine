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
