-- dialect: snowflake
-- Ridgeview, winback flow: one row per person entering on the run date (today).
-- Qualifying: completed onboarding. Core action: created a report.
-- Entry: the last core action (or qualification, if none since) was exactly 14 days before the run date, so each
-- lapse enters once. The ESP applies consent and suppression; this query only decides who crossed the threshold.
-- Holdout: holdout_bucket < 15 (salt 'ridgeview_winback_14d'); identical in every dialect and in agents/sql_dialects.holdout_bucket.
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
)
SELECT u.user_id, u.email, a.qualified_at, a.last_core_at,
       DATEDIFF(day, a.anchor_date, CURRENT_DATE()) AS days_inactive,
       u.email_consent_at IS NOT NULL AS email_consent,
       u.sms_consent_at IS NOT NULL AS sms_consent,
       MOD(TO_NUMBER(SUBSTR(MD5(u.user_id || ':ridgeview_winback_14d'), 1, 7), 'XXXXXXX'), 100) AS holdout_bucket,
       MOD(TO_NUMBER(SUBSTR(MD5(u.user_id || ':ridgeview_winback_14d'), 1, 7), 'XXXXXXX'), 100) < 15 AS in_holdout
FROM anchored a
JOIN users u ON u.user_id = a.user_id
WHERE a.anchor_date = DATEADD(day, -14, CURRENT_DATE())
ORDER BY u.user_id
