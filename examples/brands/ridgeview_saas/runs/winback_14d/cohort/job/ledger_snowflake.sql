-- dialect: snowflake
-- The ledger of win-back entries: an outbox. queue writes due lapses as 'pending', the job pushes every pending row,
-- and mark sets them to 'pushed'. A lapse is in here at most once, so nobody enters twice for one lapse. Create once.
CREATE TABLE IF NOT EXISTS lcm_flow_entries (
  flow VARCHAR NOT NULL,
  user_id VARCHAR NOT NULL,
  anchor_date DATE NOT NULL,
  holdout_bucket INTEGER NOT NULL,
  in_holdout BOOLEAN NOT NULL,
  status VARCHAR NOT NULL,
  queued_at TIMESTAMP NOT NULL,
  pushed_at TIMESTAMP,
  PRIMARY KEY (flow, user_id, anchor_date)
)
