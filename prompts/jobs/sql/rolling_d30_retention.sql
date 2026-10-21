-- dialect: postgresql
-- transaction object: not used (behavioral retention); activity = session_started, feature_used or order_completed
-- Windowed D7/D30/D60/D90 retention by monthly signup cohort, split by activation.
--
-- Definitions
--   retained at day N   any qualifying activity in the window [N, N + W) days since signup, with
--                       W = retention_window_days (30). It asks whether the person came back in that
--                       stretch at all, not whether they appeared on one exact day.
--   mature at day N     the whole window has elapsed: signed up at least N + W days before the as-of
--                       date. Every judged user gets the same observation time, so an old cohort is
--                       not flattered by having had longer to show any activity, and a cohort that
--                       signed up 40 days ago has no D30 instead of a misleadingly low one.
--   activated           reached the aha event, on or before the as-of date, within the activation
--                       window (9 days on the fixtures; see prompts/jobs/flows/activation.md).
--                       Signups still inside that window are 'activation pending', not 'not_activated'.
--   read                'readable' when at least min_judged_users are mature at D30; a retention
--                       figure on a smaller cohort is noise and is labeled as such.
--
-- Runs on the DuckDB fixtures in CI. Snowflake and BigQuery equivalents are at the bottom.

WITH params AS (
  SELECT
    DATE '2026-10-19'    AS as_of_date,          -- re-runnable for any past date
    9                    AS activation_window_days,
    30                   AS retention_window_days,
    'activation_reached' AS activation_event,
    30                   AS min_judged_users
),

signups AS (
  SELECT
    u.user_id,
    CAST(u.created_at AS DATE)                          AS signup_date,
    CAST(DATE_TRUNC('month', u.created_at) AS DATE)     AS cohort_month
  FROM users u
  CROSS JOIN params p
  WHERE CAST(u.created_at AS DATE) <= p.as_of_date
),

activity AS (
  SELECT e.user_id, CAST(e.event_at AS DATE) AS activity_date
  FROM track_events e
  WHERE e.user_id IS NOT NULL
    AND e.event_name IN ('session_started', 'feature_used', 'order_completed')
),

activation AS (
  SELECT e.user_id, MIN(CAST(e.event_at AS DATE)) AS activated_date
  FROM track_events e
  CROSS JOIN params p
  WHERE e.event_name = p.activation_event
    AND CAST(e.event_at AS DATE) <= p.as_of_date
  GROUP BY e.user_id
),

per_user AS (
  SELECT
    s.user_id,
    s.cohort_month,
    CASE WHEN p.as_of_date - s.signup_date < p.activation_window_days THEN NULL          -- still inside the window
         WHEN a.activated_date IS NOT NULL
          AND a.activated_date - s.signup_date <= p.activation_window_days THEN 1 ELSE 0 END  AS activated,
    CASE WHEN p.as_of_date - s.signup_date >= 7  + p.retention_window_days THEN 1 ELSE 0 END  AS mature_d7,
    CASE WHEN p.as_of_date - s.signup_date >= 30 + p.retention_window_days THEN 1 ELSE 0 END  AS mature_d30,
    CASE WHEN p.as_of_date - s.signup_date >= 60 + p.retention_window_days THEN 1 ELSE 0 END  AS mature_d60,
    CASE WHEN p.as_of_date - s.signup_date >= 90 + p.retention_window_days THEN 1 ELSE 0 END  AS mature_d90,
    MAX(CASE WHEN act.activity_date - s.signup_date >= 7
              AND act.activity_date - s.signup_date <  7  + p.retention_window_days THEN 1 ELSE 0 END) AS retained_d7,
    MAX(CASE WHEN act.activity_date - s.signup_date >= 30
              AND act.activity_date - s.signup_date <  30 + p.retention_window_days THEN 1 ELSE 0 END) AS retained_d30,
    MAX(CASE WHEN act.activity_date - s.signup_date >= 60
              AND act.activity_date - s.signup_date <  60 + p.retention_window_days THEN 1 ELSE 0 END) AS retained_d60,
    MAX(CASE WHEN act.activity_date - s.signup_date >= 90
              AND act.activity_date - s.signup_date <  90 + p.retention_window_days THEN 1 ELSE 0 END) AS retained_d90
  FROM signups s
  CROSS JOIN params p
  LEFT JOIN activation a   ON a.user_id = s.user_id
  LEFT JOIN activity   act ON act.user_id = s.user_id AND act.activity_date <= p.as_of_date
  GROUP BY s.user_id, s.cohort_month, s.signup_date, a.activated_date, p.activation_window_days,
           p.retention_window_days, p.as_of_date
),

by_cohort AS (
  SELECT
    cohort_month,
    activated,
    COUNT(*)                         AS cohort_size,
    SUM(mature_d7)                   AS mature_d7,
    SUM(mature_d30)                  AS mature_d30,
    SUM(mature_d60)                  AS mature_d60,
    SUM(mature_d90)                  AS mature_d90,
    SUM(retained_d7  * mature_d7)    AS retained_d7,
    SUM(retained_d30 * mature_d30)   AS retained_d30,
    SUM(retained_d60 * mature_d60)   AS retained_d60,
    SUM(retained_d90 * mature_d90)   AS retained_d90
  FROM per_user
  GROUP BY cohort_month, activated
)

SELECT
  b.cohort_month,
  CASE b.activated WHEN 1 THEN 'activated' WHEN 0 THEN 'not_activated'
                   ELSE 'activation pending' END                           AS activation_status,
  b.cohort_size,
  b.mature_d30                                                             AS users_judged_at_d30,
  ROUND(b.retained_d7  * 1.0 / NULLIF(b.mature_d7,  0), 4)                 AS d7_retention,
  ROUND(b.retained_d30 * 1.0 / NULLIF(b.mature_d30, 0), 4)                 AS d30_retention,
  ROUND(b.retained_d60 * 1.0 / NULLIF(b.mature_d60, 0), 4)                 AS d60_retention,
  ROUND(b.retained_d90 * 1.0 / NULLIF(b.mature_d90, 0), 4)                 AS d90_retention,
  CASE
    WHEN b.mature_d30 = 0                THEN 'immature'
    WHEN b.mature_d30 < p.min_judged_users THEN 'too small to read'
    ELSE 'readable'
  END                                                                      AS d30_read
FROM by_cohort b
CROSS JOIN params p
ORDER BY b.cohort_month, activation_status;

-- Snowflake: CAST(x AS DATE) and DATE_TRUNC('month', x) are unchanged; replace date subtraction
--            a - b with DATEDIFF('day', b, a).
-- BigQuery:  replace CAST(x AS DATE) with DATE(x), DATE_TRUNC('month', x) with DATE_TRUNC(DATE(x), MONTH),
--            a - b with DATE_DIFF(a, b, DAY), and x * 1.0 / NULLIF(y, 0) with SAFE_DIVIDE(x, y).
-- Classic weekly-bucket D30, if a dashboard insists on it: set retention_window_days to 7 (active in
--            days 30 to 36). Noisier, and it answers a narrower question. Never drop the upper bound:
--            "any activity on or after day N, up to today" gives older cohorts longer to qualify and
--            makes every cohort comparison drift with the as-of date.
