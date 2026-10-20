-- dialect: bigquery
-- Ledger of flow entries already pushed to the ESP. Create once; the entry query excludes what is here.
CREATE TABLE IF NOT EXISTS lcm_flow_entries (
  flow STRING NOT NULL,
  user_id STRING NOT NULL,
  anchor_date DATE NOT NULL,
  pushed_at TIMESTAMP NOT NULL,
  in_holdout BOOL NOT NULL
)
