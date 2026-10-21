-- dialect: snowflake
-- Daily step 2 of 3 for ridgeview_winback_14d: push these rows, one trigger event each (adapters/destinations/event_payloads.py).
SELECT l.user_id, u.email, l.anchor_date, DATEDIFF(day, l.anchor_date, CURRENT_DATE()) AS days_inactive,
       l.holdout_bucket, l.in_holdout
FROM lcm_flow_entries l
JOIN users u ON u.user_id = l.user_id
WHERE l.flow = 'ridgeview_winback_14d' AND l.status = 'pending'
ORDER BY l.anchor_date, l.user_id
