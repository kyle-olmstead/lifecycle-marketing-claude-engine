-- dialect: postgres
-- Ledger of flow entries already pushed to the ESP. Create once; the entry query excludes what is here.
CREATE TABLE IF NOT EXISTS lcm_flow_entries (
  flow VARCHAR NOT NULL,
  user_id VARCHAR NOT NULL,
  anchor_date DATE NOT NULL,
  pushed_at TIMESTAMP NOT NULL,
  in_holdout BOOLEAN NOT NULL,
  PRIMARY KEY (flow, user_id, anchor_date)
)
