-- dialect: snowflake
-- no-run: needs bound parameters
-- Daily step 3 of 3 for ridgeview_winback_14d: once per row whose push succeeded. Bind named parameters :user_id and :anchor_date (SQLAlchemy text() binds :name in every dialect).
UPDATE lcm_flow_entries SET status = 'pushed', pushed_at = CURRENT_TIMESTAMP()
WHERE flow = 'ridgeview_winback_14d' AND status = 'pending' AND user_id = :user_id AND anchor_date = CAST(:anchor_date AS DATE)
