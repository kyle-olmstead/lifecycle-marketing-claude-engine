-- dialect: snowflake
-- Ridgeview: threshold crossings by month (would-be entrants) and the natural return rate: the share of
-- matured crossings (at least 30 days old) whose next core action came within 30 days, with no flow running.
WITH qualified AS (
  SELECT s.user_id, MIN(s.event_at) AS qualified_at
  FROM (SELECT qe.user_id, qe.event_at FROM track_events qe WHERE qe.event_name = 'onboarding_step_completed' AND qe.properties:step_index::string = '3' AND qe.user_id IS NOT NULL) s
  WHERE CAST(s.event_at AS DATE) <= CURRENT_DATE()
  GROUP BY s.user_id
),
last_core AS (
  SELECT s.user_id, MAX(s.event_at) AS last_core_at
  FROM (SELECT ce.user_id, ce.event_at FROM track_events ce WHERE ce.event_name = 'feature_used' AND ce.properties:feature::string = 'reports' AND ce.user_id IS NOT NULL) s
  JOIN qualified q ON q.user_id = s.user_id
  WHERE CAST(s.event_at AS DATE) <= CURRENT_DATE()
  GROUP BY s.user_id
),
anchored AS (
  SELECT q.user_id, q.qualified_at, lc.last_core_at,
         CAST(GREATEST(COALESCE(lc.last_core_at, q.qualified_at), q.qualified_at) AS DATE) AS anchor_date
  FROM qualified q
  LEFT JOIN last_core lc ON lc.user_id = q.user_id
),
core_and_anchor AS (
  SELECT s.user_id, s.event_at
  FROM (SELECT ce2.user_id, ce2.event_at FROM track_events ce2 WHERE ce2.event_name = 'feature_used' AND ce2.properties:feature::string = 'reports' AND ce2.user_id IS NOT NULL) s
  JOIN qualified q ON q.user_id = s.user_id AND s.event_at >= q.qualified_at
  UNION ALL
  SELECT user_id, qualified_at AS event_at FROM qualified
),
seq AS (
  SELECT user_id, CAST(event_at AS DATE) AS d,
         LEAD(CAST(event_at AS DATE)) OVER (PARTITION BY user_id ORDER BY event_at) AS next_d
  FROM core_and_anchor
),
lapses AS (
  SELECT user_id, DATEADD(day, 14, d) AS crossed_on, next_d
  FROM seq
  WHERE (next_d IS NULL OR next_d > DATEADD(day, 14, d)) AND DATEADD(day, 14, d) <= CURRENT_DATE()
)
SELECT CAST(DATE_TRUNC('month', l.crossed_on) AS DATE) AS month,
       COUNT(*) AS entrants,
       SUM(CASE WHEN l.crossed_on <= DATEADD(day, -30, CURRENT_DATE()) THEN 1 ELSE 0 END) AS matured,
       SUM(CASE WHEN l.crossed_on <= DATEADD(day, -30, CURRENT_DATE()) AND l.next_d IS NOT NULL
                 AND l.next_d <= DATEADD(day, 30, l.crossed_on) THEN 1 ELSE 0 END) AS returned
FROM lapses l
GROUP BY 1
ORDER BY 1
