-- dialect: bigquery
-- Segment tracks into the canonical track_events table (BigQuery).
--
-- Same rules as adapters/sources/segment.py (see adapters/sources/_cdp_warehouse.py); a test runs this file,
-- transpiled to DuckDB, and the Python mapper on the same sample rows and requires identical output.
--   event_id      tracks.id (the message id); duplicates by id keep the earliest received_at
--   user_id       user_id, empty as null; anonymous_id likewise
--   event_name    the event slug lower-cased (else event_text in snake_case); application_opened -> session_started
--   event_at      timestamp, else original_timestamp, else received_at
--   properties    '{}': the tracks table holds no event properties. They live in one table per event,
--                 segment.<event>. To carry them, add one CTE per event table you need and join it on id, e.g.
--                   order_completed_props AS (SELECT o.id, TO_JSON_STRING(STRUCT(o.order_id, o.revenue)) AS properties
--                                             FROM segment.order_completed AS o)
--                 then select COALESCE(p.properties, '{}'). The Python mapper does this for every per-event CSV it reads.
-- Schema reference: https://www.twilio.com/docs/segment/connections/storage/warehouses/schema
-- Replace segment with your source's schema. Assumes timestamp, original_timestamp and received_at share one type.
WITH src AS (
  SELECT
    t.id,
    t.user_id,
    t.anonymous_id,
    COALESCE(NULLIF(LOWER(TRIM(t.event)), ''), NULLIF(LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(t.event_text, r'([a-z0-9])([A-Z])', r'\1_\2'), r'[^A-Za-z0-9]+', '_'), '_')), ''), 'unnamed_event') AS slug,
    COALESCE(t.`timestamp`, t.original_timestamp, t.received_at) AS event_at
  FROM segment.tracks AS t
  WHERE t.id IS NOT NULL AND COALESCE(t.`timestamp`, t.original_timestamp, t.received_at) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY t.received_at) = 1
)
SELECT
  id AS event_id,
  NULLIF(user_id, '') AS user_id,
  NULLIF(anonymous_id, '') AS anonymous_id,
  CASE WHEN slug = 'application_opened' THEN 'session_started' ELSE slug END AS event_name,
  event_at,
  '{}' AS properties
FROM src
