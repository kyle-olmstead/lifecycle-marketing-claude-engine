-- dialect: bigquery
-- no-run: needs bound parameters
-- Daily step 3 of 3 for ridgeview_winback_14d: once per row whose push succeeded. Bind BigQuery query parameters @user_id (STRING) and @anchor_date (DATE).
UPDATE lcm_flow_entries SET status = 'pushed', pushed_at = CURRENT_TIMESTAMP()
WHERE flow = 'ridgeview_winback_14d' AND status = 'pending' AND user_id = @user_id AND anchor_date = @anchor_date
