-- dialect: bigquery
-- Holdout property for ridgeview_winback_14d: lcm_holdout_ridgeview_winback_14d is true for the 15% holdout, by the same rule as the plan
-- (experiment.json assignment_sql). Run once, then weekly; send the results with:
--   lcm send --profile-updates <results.csv> --platform customerio --approve
SELECT user_id, email, CASE WHEN MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CONCAT(user_id, ':ridgeview_winback_14d'))), 1, 7)) AS INT64), 100) < 15 THEN 'true' ELSE 'false' END AS lcm_holdout_ridgeview_winback_14d
FROM users
WHERE email IS NOT NULL
