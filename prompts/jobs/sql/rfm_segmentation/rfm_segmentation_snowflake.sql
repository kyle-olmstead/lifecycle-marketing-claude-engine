-- dialect: snowflake
-- transaction object: orders, status = 'completed', net_amount > 0
-- customer definition: at least one completed order inside the lookback window
WITH params AS (
  SELECT
    DATE '2026-10-19' AS as_of_date,
    730               AS lookback_days,
    30                AS min_repeat_customers,
    90                AS fallback_repeat_window_days
),

orders_clean AS (
  SELECT o.user_id, o.order_id, TO_DATE(o.order_at) AS order_date, o.net_amount
  FROM orders o
  CROSS JOIN params p
  WHERE o.status = 'completed'
    AND o.net_amount > 0
    AND TO_DATE(o.order_at) <= p.as_of_date
),

sequenced AS (
  SELECT
    user_id, order_id, order_date, net_amount,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_date, order_id)                                 AS order_seq,
    DATEDIFF('day', LAG(order_date) OVER (PARTITION BY user_id ORDER BY order_date, order_id), order_date)  AS days_since_prev_order,
    DATEDIFF('day', order_date, LEAD(order_date) OVER (PARTITION BY user_id ORDER BY order_date, order_id)) AS days_to_next_order
  FROM orders_clean
),

repeat_window AS (
  SELECT
    COUNT(*)                                                         AS repeat_customers,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY days_to_next_order) AS p75_first_to_second_days
  FROM sequenced
  WHERE order_seq = 1 AND days_to_next_order IS NOT NULL
),

customers AS (
  SELECT
    s.user_id,
    MAX(s.order_date)                                                                                      AS last_order_date,
    COUNT(*)                                                                                               AS lifetime_orders,
    SUM(IFF(s.order_date > DATEADD('day', -p.lookback_days, p.as_of_date), 1, 0))                          AS orders_in_window,
    SUM(IFF(s.order_date > DATEADD('day', -p.lookback_days, p.as_of_date), s.net_amount, 0))               AS net_revenue_in_window,
    DATEDIFF('day', MAX(s.order_date), p.as_of_date)                                                       AS recency_days,
    MEDIAN(s.days_since_prev_order)                                                                        AS median_gap_days
  FROM sequenced s
  CROSS JOIN params p
  GROUP BY s.user_id, p.as_of_date, p.lookback_days
  HAVING MAX(s.order_date) > DATEADD('day', -p.lookback_days, p.as_of_date)
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
  FROM customers c
),

repeat_rule AS (
  SELECT
    IFF(rw.repeat_customers >= p.min_repeat_customers, CEIL(rw.p75_first_to_second_days), p.fallback_repeat_window_days) AS window_days,
    IFF(rw.repeat_customers >= p.min_repeat_customers, 'p75 first-to-second-order gap', 'fallback: too few repeat customers') AS window_source
  FROM repeat_window rw
  CROSS JOIN params p
),

labelled AS (
  SELECT
    s.*,
    w.window_days                                                                  AS repeat_window_days,
    w.window_source                                                                AS repeat_window_source,
    COALESCE(s.recency_days > 2 * s.median_gap_days, FALSE)                        AS overdue_vs_own_cycle,
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
    END                                                                            AS rfm_segment
  FROM scored s
  CROSS JOIN repeat_rule w
)

SELECT
  l.user_id,
  LOWER(TRIM(u.email))                                          AS email,
  l.rfm_segment,
  l.r_score, l.f_score, l.m_score,
  l.recency_days, l.orders_in_window, l.net_revenue_in_window, l.lifetime_orders,
  l.overdue_vs_own_cycle, l.repeat_window_days, l.repeat_window_source,
  p.as_of_date                                                  AS scored_on,
  FLOOR((ROW_NUMBER() OVER (ORDER BY l.user_id) - 1) / 100)     AS hubspot_batch,
  FLOOR((ROW_NUMBER() OVER (ORDER BY l.user_id) - 1) / 10000)   AS klaviyo_job,
  OBJECT_CONSTRUCT(
    'idProperty', 'email',
    'id',         LOWER(TRIM(u.email)),
    'properties', OBJECT_CONSTRUCT(
      'lmm_rfm_segment',   l.rfm_segment,
      'lmm_rfm_r',         l.r_score,
      'lmm_rfm_f',         l.f_score,
      'lmm_rfm_m',         l.m_score,
      'lmm_rfm_scored_on', TO_CHAR(p.as_of_date, 'YYYY-MM-DD')
    )
  )                                                             AS hubspot_input,
  OBJECT_CONSTRUCT(
    'type', 'profile',
    'attributes', OBJECT_CONSTRUCT(
      'email',      LOWER(TRIM(u.email)),
      'properties', OBJECT_CONSTRUCT(
        'lmm_rfm_segment',   l.rfm_segment,
        'lmm_rfm_r',         l.r_score,
        'lmm_rfm_f',         l.f_score,
        'lmm_rfm_m',         l.m_score,
        'lmm_rfm_scored_on', TO_CHAR(p.as_of_date, 'YYYY-MM-DD')
      )
    )
  )                                                             AS klaviyo_profile
FROM labelled l
JOIN users u ON u.user_id = l.user_id
CROSS JOIN params p
WHERE NULLIF(TRIM(u.email), '') IS NOT NULL
ORDER BY l.user_id;
