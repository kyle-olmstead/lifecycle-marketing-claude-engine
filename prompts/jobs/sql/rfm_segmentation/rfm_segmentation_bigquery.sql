-- dialect: bigquery
-- transaction object: orders, status = 'completed', net_amount > 0
-- customer definition: at least one completed order inside the lookback window
-- qualify every table with your dataset (`project.dataset.orders`); names are left bare here
WITH params AS (
  SELECT
    DATE '2026-10-19' AS as_of_date,
    730               AS lookback_days,
    30                AS min_repeat_customers,
    90                AS fallback_repeat_window_days
),

orders_clean AS (
  SELECT o.user_id, o.order_id, DATE(o.order_at) AS order_date, o.net_amount
  FROM orders AS o
  CROSS JOIN params AS p
  WHERE o.status = 'completed'
    AND o.net_amount > 0
    AND DATE(o.order_at) <= p.as_of_date
),

sequenced AS (
  SELECT
    user_id, order_id, order_date, net_amount,
    ROW_NUMBER() OVER w                                         AS order_seq,
    DATE_DIFF(order_date, LAG(order_date) OVER w, DAY)          AS days_since_prev_order,
    DATE_DIFF(LEAD(order_date) OVER w, order_date, DAY)         AS days_to_next_order
  FROM orders_clean
  WINDOW w AS (PARTITION BY user_id ORDER BY order_date, order_id)
),

repeat_window AS (
  SELECT
    COUNT(*)                                                            AS repeat_customers,
    APPROX_QUANTILES(days_to_next_order, 100)[SAFE_OFFSET(75)]          AS p75_first_to_second_days
  FROM sequenced
  WHERE order_seq = 1 AND days_to_next_order IS NOT NULL
),

customers AS (
  SELECT
    s.user_id,
    MAX(s.order_date)                                                                                             AS last_order_date,
    COUNT(*)                                                                                                      AS lifetime_orders,
    SUM(IF(s.order_date > DATE_SUB(p.as_of_date, INTERVAL p.lookback_days DAY), 1, 0))                            AS orders_in_window,
    SUM(IF(s.order_date > DATE_SUB(p.as_of_date, INTERVAL p.lookback_days DAY), s.net_amount, 0))                 AS net_revenue_in_window,
    DATE_DIFF(ANY_VALUE(p.as_of_date), MAX(s.order_date), DAY)                                                    AS recency_days,
    APPROX_QUANTILES(s.days_since_prev_order, 2)[SAFE_OFFSET(1)]                                                  AS median_gap_days
  FROM sequenced AS s
  CROSS JOIN params AS p
  GROUP BY s.user_id
  HAVING MAX(s.order_date) > DATE_SUB(ANY_VALUE(p.as_of_date), INTERVAL ANY_VALUE(p.lookback_days) DAY)
),

scored AS (
  SELECT
    c.*,
    NTILE(5) OVER (ORDER BY c.recency_days DESC, c.user_id)     AS r_score,
    CASE
      WHEN c.orders_in_window >= 6 THEN 5
      WHEN c.orders_in_window >= 4 THEN 4
      WHEN c.orders_in_window = 3  THEN 3
      WHEN c.orders_in_window = 2  THEN 2
      ELSE 1
    END                                                         AS f_score,
    NTILE(5) OVER (ORDER BY c.net_revenue_in_window, c.user_id) AS m_score
  FROM customers AS c
),

repeat_rule AS (
  SELECT
    IF(rw.repeat_customers >= p.min_repeat_customers, CAST(CEIL(rw.p75_first_to_second_days) AS INT64), p.fallback_repeat_window_days) AS window_days,
    IF(rw.repeat_customers >= p.min_repeat_customers, 'p75 first-to-second-order gap', 'fallback: too few repeat customers')          AS window_source
  FROM repeat_window AS rw
  CROSS JOIN params AS p
),

labelled AS (
  SELECT
    s.*,
    w.window_days                                                   AS repeat_window_days,
    w.window_source                                                 AS repeat_window_source,
    IFNULL(s.recency_days > 2 * s.median_gap_days, FALSE)           AS overdue_vs_own_cycle,
    CASE
      WHEN s.lifetime_orders = 1 AND s.recency_days > w.window_days            THEN 'instant_churn'
      WHEN s.r_score >= 4 AND s.f_score >= 4 AND s.m_score >= 4                THEN 'champions'
      WHEN s.r_score >= 3 AND s.f_score >= 4                                   THEN 'loyal'
      WHEN s.r_score <= 2 AND s.f_score >= 4 AND s.m_score >= 4                THEN 'cannot_lose'
      WHEN s.r_score <= 2 AND s.f_score >= 3                                   THEN 'at_risk'
      WHEN s.r_score = 5  AND s.f_score = 1                                    THEN 'new_customers'
      WHEN s.r_score >= 4 AND s.f_score IN (2, 3)                              THEN 'potential_loyalists'
      WHEN s.r_score = 4  AND s.f_score = 1                                    THEN 'promising'
      WHEN s.r_score = 3  AND s.f_score IN (2, 3)                              THEN 'need_attention'
      WHEN s.r_score = 3  AND s.f_score = 1                                    THEN 'about_to_sleep'
      WHEN s.r_score <= 2 AND s.f_score <= 2                                   THEN 'hibernating'
      ELSE 'unclassified'
    END                                                             AS rfm_segment
  FROM scored AS s
  CROSS JOIN repeat_rule AS w
)

SELECT
  l.user_id,
  LOWER(TRIM(u.email))                                    AS email,
  l.rfm_segment,
  l.r_score, l.f_score, l.m_score,
  l.recency_days, l.orders_in_window, l.net_revenue_in_window, l.lifetime_orders,
  l.overdue_vs_own_cycle, l.repeat_window_days, l.repeat_window_source,
  p.as_of_date                                            AS scored_on,
  DIV(ROW_NUMBER() OVER (ORDER BY l.user_id) - 1, 100)    AS hubspot_batch,
  DIV(ROW_NUMBER() OVER (ORDER BY l.user_id) - 1, 10000)  AS klaviyo_job,
  TO_JSON_STRING(STRUCT(
    'email'                AS idProperty,
    LOWER(TRIM(u.email))   AS id,
    STRUCT(
      l.rfm_segment                         AS lmm_rfm_segment,
      l.r_score                             AS lmm_rfm_r,
      l.f_score                             AS lmm_rfm_f,
      l.m_score                             AS lmm_rfm_m,
      FORMAT_DATE('%Y-%m-%d', p.as_of_date) AS lmm_rfm_scored_on
    )                      AS properties
  ))                                                      AS hubspot_input,
  TO_JSON_STRING(STRUCT(
    'profile' AS type,
    STRUCT(
      LOWER(TRIM(u.email)) AS email,
      STRUCT(
        l.rfm_segment                         AS lmm_rfm_segment,
        l.r_score                             AS lmm_rfm_r,
        l.f_score                             AS lmm_rfm_f,
        l.m_score                             AS lmm_rfm_m,
        FORMAT_DATE('%Y-%m-%d', p.as_of_date) AS lmm_rfm_scored_on
      ) AS properties
    ) AS attributes
  ))                                                      AS klaviyo_profile
FROM labelled AS l
JOIN users AS u ON u.user_id = l.user_id
CROSS JOIN params AS p
WHERE NULLIF(TRIM(u.email), '') IS NOT NULL
ORDER BY l.user_id;
