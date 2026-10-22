-- dialect: duckdb
-- Northfield Coffee Roasters, abandoned cart flow: who abandoned in the 24 hours before the run (audit and backfill of the native trigger)
-- Trigger: started checkout (checkout_started). Goal: placed an order (order_completed).
-- Entry: checkout_started at least 1 hour(s) ago with no order_completed since; one per person per day.
WITH triggers AS (
  SELECT t.user_id, MIN(t.event_at) AS started_at, MIN(json_extract_string(t.properties, '$.checkout_id')) AS ref
  FROM track_events t
  WHERE t.event_name = 'checkout_started' AND t.user_id IS NOT NULL AND t.event_at <= TIMESTAMP '2026-10-18 23:59:59'
  GROUP BY t.user_id, CAST(t.event_at AS DATE)
),
stops AS (
  SELECT s0.user_id, s0.order_at AS event_at, s0.order_id AS ref FROM orders s0 WHERE s0.status = 'completed' AND s0.user_id IS NOT NULL
),
abandoned AS (
  SELECT tr.user_id, tr.started_at, tr.ref, MIN(st.event_at) AS next_stop_at
  FROM triggers tr
  LEFT JOIN stops st ON st.user_id = tr.user_id AND st.event_at >= tr.started_at
  GROUP BY tr.user_id, tr.started_at, tr.ref
  HAVING MIN(st.event_at) IS NULL OR MIN(st.event_at) > (tr.started_at + INTERVAL '60 minutes')
)
SELECT u.user_id, u.email, a.started_at, a.ref, (date_diff('minute', a.started_at, TIMESTAMP '2026-10-18 23:59:59') / 60.0) AS hours_since,
       u.email_consent_at IS NOT NULL AS email_consent,
       u.sms_consent_at IS NOT NULL AS sms_consent,
       (CAST('0x' || substr(md5(u.user_id || ':northfield_coffee_cart_abandon_1h'), 1, 7) AS INTEGER) % 100) AS holdout_bucket,
       (CAST('0x' || substr(md5(u.user_id || ':northfield_coffee_cart_abandon_1h'), 1, 7) AS INTEGER) % 100) < 10 AS in_holdout
FROM abandoned a
JOIN users u ON u.user_id = a.user_id
WHERE a.started_at <= (TIMESTAMP '2026-10-18 23:59:59' + INTERVAL '-60 minutes') AND a.started_at > (TIMESTAMP '2026-10-18 23:59:59' + INTERVAL '-1500 minutes')
  AND (a.next_stop_at IS NULL OR a.next_stop_at > TIMESTAMP '2026-10-18 23:59:59')
  AND (CAST('0x' || substr(md5(u.user_id || ':lmm_global'), 1, 7) AS INTEGER) % 100) >= 5
ORDER BY u.user_id
