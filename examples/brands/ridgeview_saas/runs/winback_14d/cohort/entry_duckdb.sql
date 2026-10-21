-- dialect: duckdb
-- Ridgeview, winback flow: who is due on the run date (the literal as-of date).
-- Qualifying: completed onboarding (onboarding_step_completed where step_index = 3). Core action: created a report (feature_used where feature = reports).
-- Entry: 14 calendar days or more since the last core action (or since qualifying, if none since): the anchor date is
-- between 14 and 17 days before the run date, and this lapse (user, anchor date) is not yet in lcm_flow_entries.
-- A missed run catches up for 3 days. The ESP applies consent and suppression; this query decides who crossed.
-- Holdout: holdout_bucket < 15 (salt 'ridgeview_winback_14d'); identical in every dialect and in engine/sql/dialects.holdout_bucket.
WITH qualified AS (
  SELECT s.user_id, MIN(s.event_at) AS qualified_at
  FROM (SELECT qe.user_id, qe.event_at FROM track_events qe WHERE qe.event_name = 'onboarding_step_completed' AND json_extract_string(qe.properties, '$.step_index') = '3' AND qe.user_id IS NOT NULL) s
  WHERE CAST(s.event_at AS DATE) <= DATE '2026-10-18'
  GROUP BY s.user_id
),
last_core AS (
  SELECT s.user_id, MAX(s.event_at) AS last_core_at
  FROM (SELECT ce.user_id, ce.event_at FROM track_events ce WHERE ce.event_name = 'feature_used' AND json_extract_string(ce.properties, '$.feature') = 'reports' AND ce.user_id IS NOT NULL) s
  JOIN qualified q ON q.user_id = s.user_id
  WHERE CAST(s.event_at AS DATE) <= DATE '2026-10-18'
  GROUP BY s.user_id
),
anchored AS (
  SELECT q.user_id, q.qualified_at, lc.last_core_at,
         CAST(GREATEST(COALESCE(lc.last_core_at, q.qualified_at), q.qualified_at) AS DATE) AS anchor_date
  FROM qualified q
  LEFT JOIN last_core lc ON lc.user_id = q.user_id
)
SELECT u.user_id, u.email, a.qualified_at, a.last_core_at, a.anchor_date,
       (DATE '2026-10-18' - a.anchor_date) AS days_inactive,
       u.email_consent_at IS NOT NULL AS email_consent,
       u.sms_consent_at IS NOT NULL AS sms_consent,
       (CAST('0x' || substr(md5(u.user_id || ':ridgeview_winback_14d'), 1, 7) AS INTEGER) % 100) AS holdout_bucket,
       (CAST('0x' || substr(md5(u.user_id || ':ridgeview_winback_14d'), 1, 7) AS INTEGER) % 100) < 15 AS in_holdout
FROM anchored a
JOIN users u ON u.user_id = a.user_id
LEFT JOIN lcm_flow_entries l ON l.flow = 'ridgeview_winback_14d' AND l.user_id = a.user_id AND l.anchor_date = a.anchor_date
WHERE a.anchor_date BETWEEN (DATE '2026-10-18' - 17) AND (DATE '2026-10-18' - 14)
  AND l.user_id IS NULL
ORDER BY u.user_id
