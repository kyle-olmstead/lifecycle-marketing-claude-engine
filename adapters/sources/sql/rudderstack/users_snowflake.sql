-- dialect: snowflake
-- RudderStack identifies into the canonical users table (Snowflake).
--
-- Same rules as adapters/sources/rudderstack.py (see adapters/sources/_cdp_warehouse.py); a test runs this file,
-- transpiled to DuckDB, and the Python mapper on the same sample rows and requires identical output.
--   user_id    identifies.user_id
--   email      the latest non-empty email trait, trimmed and lower-cased; users without one are left out
--   created_at the latest created_at trait, else the first identify call
--   country    the latest country trait when it is a two-letter code, upper-cased; names are left empty
--   plan       the latest plan trait
--   timezone   the latest non-empty context_timezone
--   "latest" orders identify calls by timestamp (else original_timestamp, else received_at), then received_at.
-- Schema reference: https://www.rudderstack.com/docs/destinations/warehouse-destinations/warehouse-schema/
-- Replace rudderstack with your source's schema. A trait column exists only once some call sent it: delete the line
-- of any trait (country, plan, created_at) your source has never sent, or the query will not compile.
WITH ids AS (
  SELECT i.*, COALESCE(i.timestamp, i.original_timestamp, i.received_at) AS seen_at
  FROM rudderstack.identifies AS i
  WHERE i.user_id IS NOT NULL AND i.user_id <> '' AND COALESCE(i.timestamp, i.original_timestamp, i.received_at) IS NOT NULL
),
traits AS (
  SELECT DISTINCT
    user_id,
    FIRST_VALUE(NULLIF(TRIM(email), '')) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS email,
    FIRST_VALUE(NULLIF(TRIM(plan), '')) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS plan,
    FIRST_VALUE(NULLIF(TRIM(country), '')) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS country,
    FIRST_VALUE(created_at) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS created_at_trait,
    FIRST_VALUE(NULLIF(TRIM(context_timezone), '')) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS timezone,
    MIN(seen_at) OVER (PARTITION BY user_id) AS first_seen_at
  FROM ids
)
SELECT
  user_id,
  LOWER(email) AS email,
  COALESCE(TRY_CAST(TO_VARCHAR(created_at_trait) AS TIMESTAMP_TZ), first_seen_at) AS created_at,
  CASE WHEN LENGTH(country) = 2 THEN UPPER(country) END AS country,
  plan,
  timezone
FROM traits
WHERE email IS NOT NULL
