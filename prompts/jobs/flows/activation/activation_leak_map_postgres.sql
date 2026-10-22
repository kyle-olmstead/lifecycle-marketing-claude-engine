-- dialect: postgresql
-- leak map: where signups stall on the way to the aha event, and how long each step takes
WITH params AS (
  SELECT DATE '2026-10-19' AS as_of_date, 9 AS activation_window_days
),
signups AS (
  SELECT e.user_id, MIN(e.event_at) AS signed_up_at
  FROM track_events e
  WHERE e.event_name = 'signed_up' AND e.user_id IS NOT NULL
  GROUP BY e.user_id
),
steps AS (
  SELECT e.user_id, e.properties ->> 'step' AS step, MIN(e.event_at) AS done_at
  FROM track_events e
  WHERE e.event_name = 'onboarding_step_completed'
  GROUP BY e.user_id, e.properties ->> 'step'
),
activation AS (
  SELECT user_id, MIN(event_at) AS activated_at
  FROM track_events
  WHERE event_name = 'activation_reached'
  GROUP BY user_id
),
per_user AS (
  SELECT
    s.user_id,
    s.signed_up_at,
    MAX(CASE WHEN st.step = 'connected_data_source' THEN st.done_at END) AS step1_at,
    MAX(CASE WHEN st.step = 'invited_teammate'      THEN st.done_at END) AS step2_at,
    MAX(CASE WHEN st.step = 'created_first_report'  THEN st.done_at END) AS step3_at,
    MAX(a.activated_at)                                                   AS activated_at
  FROM signups s
  CROSS JOIN params p
  LEFT JOIN steps st     ON st.user_id = s.user_id
  LEFT JOIN activation a ON a.user_id  = s.user_id
  WHERE CAST(s.signed_up_at AS DATE) <= p.as_of_date - p.activation_window_days   -- mature cohorts only
  GROUP BY s.user_id, s.signed_up_at
)
SELECT
  COUNT(*)                                                                                   AS mature_signups,
  ROUND(AVG(CASE WHEN step1_at IS NOT NULL THEN 1.0 ELSE 0 END), 3)                          AS reached_connected_data_source,
  ROUND(AVG(CASE WHEN step2_at IS NOT NULL THEN 1.0 ELSE 0 END), 3)                          AS reached_invited_teammate,
  ROUND(AVG(CASE WHEN step3_at IS NOT NULL THEN 1.0 ELSE 0 END), 3)                          AS reached_created_first_report,
  ROUND(AVG(CASE WHEN activated_at IS NOT NULL
                  AND CAST(activated_at AS DATE) - CAST(signed_up_at AS DATE) <= 9 THEN 1.0 ELSE 0 END), 3) AS activated_within_9d,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (step1_at - signed_up_at)) / 3600.0) AS median_hours_to_step1,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (activated_at - signed_up_at)) / 3600.0) AS median_hours_to_activation
FROM per_user;
