-- dialect: postgres
-- Daily step 3 of 3 for ridgeview_winback_14d: run only when every push in step 2 succeeded.
UPDATE lcm_flow_entries SET status = 'pushed', pushed_at = CURRENT_TIMESTAMP
WHERE flow = 'ridgeview_winback_14d' AND status = 'pending'
