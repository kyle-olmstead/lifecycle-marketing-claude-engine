-- dialect: postgresql
-- activation window from data: the 75th percentile of days from signup to the aha, among users who
-- reach it inside the 30-day cap, on cohorts that have all had the full 30 days. Including recent
-- signups would count only their fast activators and pull the percentile down.
WITH params AS (
  SELECT TIMESTAMP '2026-10-19 00:00:00' AS as_of_ts
),
signups AS (
  SELECT e.user_id, MIN(e.event_at) AS signed_up_at
  FROM track_events e
  CROSS JOIN params p
  WHERE e.event_name = 'signed_up' AND e.user_id IS NOT NULL AND e.event_at < p.as_of_ts
  GROUP BY e.user_id
),
aha AS (
  SELECT e.user_id, MIN(e.event_at) AS activated_at
  FROM track_events e
  CROSS JOIN params p
  WHERE e.event_name = 'activation_reached' AND e.event_at < p.as_of_ts
  GROUP BY e.user_id
),
mature AS (
  SELECT s.user_id, s.signed_up_at
  FROM signups s
  CROSS JOIN params p
  WHERE s.signed_up_at <= p.as_of_ts - INTERVAL '30 days'   -- every user here had the whole cap to activate
),
timed AS (
  SELECT m.user_id, EXTRACT(EPOCH FROM (a.activated_at - m.signed_up_at)) / 86400.0 AS days_to_aha
  FROM mature m
  LEFT JOIN aha a ON a.user_id = m.user_id AND a.activated_at < m.signed_up_at + INTERVAL '30 days'
)
SELECT
  COUNT(*)                                                                  AS mature_signups,
  COUNT(days_to_aha)                                                        AS activated_within_cap,
  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY days_to_aha)                 AS p50_days_to_aha,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY days_to_aha)                 AS p75_days_to_aha,
  LEAST(30, CEIL(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY days_to_aha))) AS activation_window_days
FROM timed;
