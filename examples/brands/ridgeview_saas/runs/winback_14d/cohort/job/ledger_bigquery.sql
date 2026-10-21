-- dialect: bigquery
-- The ledger of win-back entries: an outbox. queue writes due lapses as 'pending', the job pushes every pending row,
-- and mark sets them to 'pushed'. A lapse is in here at most once, so nobody enters twice for one lapse. Create once.
CREATE TABLE IF NOT EXISTS lcm_flow_entries (
  flow STRING NOT NULL,
  user_id STRING NOT NULL,
  anchor_date DATE NOT NULL,
  holdout_bucket INT64 NOT NULL,
  in_holdout BOOL NOT NULL,
  status STRING NOT NULL,
  queued_at TIMESTAMP NOT NULL,
  pushed_at TIMESTAMP,
  detail STRING
)
-- Upgrading from v2: ALTER TABLE lcm_flow_entries ADD COLUMN detail STRING;
