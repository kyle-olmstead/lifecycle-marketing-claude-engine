-- dialect: bigquery
-- Northfield Coffee Roasters, abandoned cart flow: abandoners by month and the share who reached the goal within 10 days with no flow
-- Trigger: started checkout (checkout_started). Goal: placed an order (order_completed).
WITH triggers AS (
  SELECT t.user_id, MIN(t.event_at) AS started_at, MIN(JSON_VALUE(t.properties, '$.checkout_id')) AS ref
  FROM track_events t
  WHERE t.event_name = 'checkout_started' AND t.user_id IS NOT NULL AND t.event_at <= CURRENT_TIMESTAMP()
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
  HAVING MIN(st.event_at) IS NULL OR MIN(st.event_at) > TIMESTAMP_ADD(tr.started_at, INTERVAL 60 MINUTE)
),
goal AS (
  SELECT g.user_id, g.order_at AS event_at, g.order_id AS ref FROM orders g WHERE g.status = 'completed' AND g.user_id IS NOT NULL
),
recovered AS (
  SELECT a.user_id, a.started_at, MIN(g.event_at) AS goal_at
  FROM abandoned a
  LEFT JOIN goal g ON g.user_id = a.user_id AND g.event_at >= a.started_at
  GROUP BY a.user_id, a.started_at
)
SELECT CAST(DATE_TRUNC(r.started_at, MONTH) AS DATE) AS month,
       COUNT(*) AS entrants,
       SUM(CASE WHEN r.started_at <= TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL -14400 MINUTE) THEN 1 ELSE 0 END) AS matured,
       SUM(CASE WHEN r.started_at <= TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL -14400 MINUTE) AND r.goal_at IS NOT NULL
                 AND r.goal_at <= TIMESTAMP_ADD(r.started_at, INTERVAL 14400 MINUTE) THEN 1 ELSE 0 END) AS returned
FROM recovered r
GROUP BY 1
ORDER BY 1
