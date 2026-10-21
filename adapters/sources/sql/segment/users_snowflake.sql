-- dialect: snowflake
-- Segment users and identifies into the canonical users table (Snowflake).
--
-- Same rules as adapters/sources/segment.py (see adapters/sources/_cdp_warehouse.py); a test runs this file,
-- transpiled to DuckDB, and the Python mapper on the same sample rows and requires identical output.
--   user_id    users.id (Segment: the users table is upserted on user_id and holds the latest traits)
--   email      the email trait, trimmed and lower-cased; users without one are left out (email is required)
--   created_at the created_at trait, else the first identify, else users.received_at
--   country    the country trait when it is a two-letter code, upper-cased; names are left empty
--   plan       the plan trait
--   timezone   the latest non-empty context_timezone across identify calls
-- Schema reference: https://www.twilio.com/docs/segment/connections/storage/warehouses/schema
-- Replace segment with your source's schema. A trait column exists only once some call sent it: delete the line of
-- any trait (country, plan, created_at) your source has never sent, or the query will not compile.
WITH ids AS (
  SELECT i.*, COALESCE(i.timestamp, i.original_timestamp, i.received_at) AS seen_at
  FROM segment.identifies AS i
  WHERE i.user_id IS NOT NULL AND i.user_id <> '' AND COALESCE(i.timestamp, i.original_timestamp, i.received_at) IS NOT NULL
),
id_traits AS (
  SELECT DISTINCT
    user_id,
    FIRST_VALUE(NULLIF(TRIM(context_timezone), '')) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY seen_at DESC, COALESCE(received_at, seen_at) DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS timezone,
    MIN(seen_at) OVER (PARTITION BY user_id) AS first_seen_at
  FROM ids
)
SELECT
  u.id AS user_id,
  LOWER(TRIM(u.email)) AS email,
  COALESCE(TRY_CAST(TO_VARCHAR(u.created_at) AS TIMESTAMP_TZ), t.first_seen_at, u.received_at) AS created_at,
  CASE WHEN LENGTH(TRIM(u.country)) = 2 THEN UPPER(TRIM(u.country)) END AS country,
  NULLIF(TRIM(u.plan), '') AS plan,
  t.timezone
FROM segment.users AS u
LEFT JOIN id_traits AS t ON t.user_id = u.id
WHERE u.id IS NOT NULL AND u.email IS NOT NULL AND TRIM(u.email) <> ''
  AND COALESCE(TRY_CAST(TO_VARCHAR(u.created_at) AS TIMESTAMP_TZ), t.first_seen_at, u.received_at) IS NOT NULL
