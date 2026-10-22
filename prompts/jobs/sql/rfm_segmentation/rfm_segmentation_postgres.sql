-- dialect: postgresql
-- transaction object: orders, status = 'completed', net_amount > 0 (refunds and cancellations excluded)
-- customer definition: at least one completed order inside the lookback window; lifecycle_stage is not used
-- scored as of: params.as_of_date, so the run can be repeated for any past date
WITH params AS (
  SELECT
    DATE '2026-10-19' AS as_of_date,
    730               AS lookback_days,                -- two years: long enough for a full repeat cycle
    30                AS min_repeat_customers,         -- below this, the repeat window is not estimated from data
    90                AS fallback_repeat_window_days   -- used only when the data cannot estimate it; say so in the output
),

orders_clean AS (
  SELECT o.user_id, o.order_id, CAST(o.order_at AS DATE) AS order_date, o.net_amount
  FROM orders o
  CROSS JOIN params p
  WHERE o.status = 'completed'
    AND o.net_amount > 0
    AND CAST(o.order_at AS DATE) <= p.as_of_date
),

-- LAG and LEAD over each customer's full history
sequenced AS (
  SELECT
    user_id, order_id, order_date, net_amount,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_date, order_id)                        AS order_seq,
    order_date - LAG(order_date) OVER (PARTITION BY user_id ORDER BY order_date, order_id)         AS days_since_prev_order,
    LEAD(order_date) OVER (PARTITION BY user_id ORDER BY order_date, order_id) - order_date        AS days_to_next_order
  FROM orders_clean
),

-- how long a customer who does come back takes to place a second order
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
    MAX(s.order_date)                                                                          AS last_order_date,
    COUNT(*)                                                                                   AS lifetime_orders,
    SUM(CASE WHEN s.order_date > p.as_of_date - p.lookback_days THEN 1 ELSE 0 END)            AS orders_in_window,
    SUM(CASE WHEN s.order_date > p.as_of_date - p.lookback_days THEN s.net_amount ELSE 0 END) AS net_revenue_in_window,
    p.as_of_date - MAX(s.order_date)                                                           AS recency_days,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.days_since_prev_order)                       AS median_gap_days
  FROM sequenced s
  CROSS JOIN params p
  GROUP BY s.user_id, p.as_of_date, p.lookback_days
  HAVING MAX(s.order_date) > p.as_of_date - p.lookback_days
),

scored AS (
  SELECT
    c.*,
    NTILE(5) OVER (ORDER BY c.recency_days DESC, c.user_id)          AS r_score,   -- most recent buyers score 5
    CASE                                                                            -- fixed bands: see "Why F is banded"
      WHEN c.orders_in_window >= 6 THEN 5
      WHEN c.orders_in_window >= 4 THEN 4
      WHEN c.orders_in_window = 3  THEN 3
      WHEN c.orders_in_window = 2  THEN 2
      ELSE 1
    END                                                              AS f_score,
    NTILE(5) OVER (ORDER BY c.net_revenue_in_window, c.user_id)      AS m_score    -- highest net revenue scores 5
  FROM customers c
),

labelled AS (
  SELECT
    s.*,
    w.window_days                                                                  AS repeat_window_days,
    w.window_source                                                                AS repeat_window_source,
    (s.median_gap_days IS NOT NULL AND s.recency_days > 2 * s.median_gap_days)     AS overdue_vs_own_cycle,
    CASE                                                                           -- first match wins; the order is the design
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
  CROSS JOIN (
    SELECT
      CASE WHEN rw.repeat_customers >= p.min_repeat_customers
           THEN CEIL(rw.p75_first_to_second_days) ELSE p.fallback_repeat_window_days END   AS window_days,
      CASE WHEN rw.repeat_customers >= p.min_repeat_customers
           THEN 'p75 first-to-second-order gap' ELSE 'fallback: too few repeat customers' END AS window_source
    FROM repeat_window rw CROSS JOIN params p
  ) w
)

SELECT
  l.user_id,
  LOWER(TRIM(u.email))                     AS email,
  l.rfm_segment,
  l.r_score, l.f_score, l.m_score,
  l.recency_days, l.orders_in_window, l.net_revenue_in_window, l.lifetime_orders,
  l.overdue_vs_own_cycle,
  l.repeat_window_days, l.repeat_window_source,
  (SELECT as_of_date FROM params)          AS scored_on,
  FLOOR((ROW_NUMBER() OVER (ORDER BY l.user_id) - 1) / 100)   AS hubspot_batch,   -- 100 inputs per batch upsert
  FLOOR((ROW_NUMBER() OVER (ORDER BY l.user_id) - 1) / 10000) AS klaviyo_job,     -- 10,000 profiles per bulk import job
  json_build_object(
    'idProperty', 'email',
    'id',         LOWER(TRIM(u.email)),
    'properties', json_build_object(
      'lmm_rfm_segment',   l.rfm_segment,
      'lmm_rfm_r',         l.r_score,
      'lmm_rfm_f',         l.f_score,
      'lmm_rfm_m',         l.m_score,
      'lmm_rfm_scored_on', (SELECT as_of_date FROM params)
    )
  )                                        AS hubspot_input,
  json_build_object(
    'type', 'profile',
    'attributes', json_build_object(
      'email',      LOWER(TRIM(u.email)),
      'properties', json_build_object(
        'lmm_rfm_segment',   l.rfm_segment,
        'lmm_rfm_r',         l.r_score,
        'lmm_rfm_f',         l.f_score,
        'lmm_rfm_m',         l.m_score,
        'lmm_rfm_scored_on', (SELECT as_of_date FROM params)
      )
    )
  )                                        AS klaviyo_profile
FROM labelled l
JOIN users u ON u.user_id = l.user_id
WHERE u.email IS NOT NULL AND u.email <> ''
ORDER BY l.user_id;
