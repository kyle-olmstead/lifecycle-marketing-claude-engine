-- dialect: bigquery
-- Daily step 1 of 3 for ridgeview_winback_14d: queue every lapse that is due as 'pending'.
INSERT INTO lcm_flow_entries (flow, user_id, anchor_date, holdout_bucket, in_holdout, status, queued_at)
SELECT 'ridgeview_winback_14d', e.user_id, e.anchor_date, e.holdout_bucket, e.in_holdout, 'pending', CURRENT_TIMESTAMP()
FROM (
WITH qualified AS (
  SELECT s.user_id, MIN(s.event_at) AS qualified_at
  FROM (SELECT qe.user_id, qe.event_at FROM track_events qe WHERE qe.event_name = 'onboarding_step_completed' AND JSON_VALUE(qe.properties, '$.step_index') = '3' AND qe.user_id IS NOT NULL) s
  WHERE CAST(s.event_at AS DATE) <= CURRENT_DATE()
  GROUP BY s.user_id
),
last_core AS (
  SELECT s.user_id, MAX(s.event_at) AS last_core_at
  FROM (SELECT ce.user_id, ce.event_at FROM track_events ce WHERE ce.event_name = 'feature_used' AND JSON_VALUE(ce.properties, '$.feature') = 'reports' AND ce.user_id IS NOT NULL) s
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
SELECT u.user_id, u.email, a.qualified_at, a.last_core_at, a.anchor_date,
       DATE_DIFF(CURRENT_DATE(), a.anchor_date, DAY) AS days_inactive,
       u.email_consent_at IS NOT NULL AS email_consent,
       u.sms_consent_at IS NOT NULL AS sms_consent,
       MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CONCAT(u.user_id, ':ridgeview_winback_14d'))), 1, 7)) AS INT64), 100) AS holdout_bucket,
       MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CONCAT(u.user_id, ':ridgeview_winback_14d'))), 1, 7)) AS INT64), 100) < 15 AS in_holdout
FROM anchored a
JOIN users u ON u.user_id = a.user_id
LEFT JOIN lcm_flow_entries l ON l.flow = 'ridgeview_winback_14d' AND l.user_id = a.user_id AND l.anchor_date = a.anchor_date
WHERE a.anchor_date BETWEEN DATE_ADD(CURRENT_DATE(), INTERVAL -17 DAY) AND DATE_ADD(CURRENT_DATE(), INTERVAL -14 DAY)
  AND l.user_id IS NULL
) e
