-- dialect: bigquery
-- GA4 BigQuery export (events_YYYYMMDD) flattened into the canonical track_events table.
--
-- Same rules as adapters/sources/ga4_bigquery.py, which runs them on an offline JSONL or Parquet dump; a test runs
-- this file (transpiled to DuckDB) and the Python mapper on the same rows and requires identical output.
--
--   event_id     purchase:<transaction_id> for a purchase that carries one (GA4 often logs a purchase twice when a
--                thank-you page reloads; one transaction is one order), else ga4:<md5 hex> of
--                user_pseudo_id | event_timestamp | event_name | event_bundle_sequence_id | batch_event_index.
--                GA4 has no event id of its own. Rows sharing an id are duplicates; the earliest is kept.
--   user_id      user_id (empty for events before the user signed in)
--   anonymous_id user_pseudo_id
--   event_name   purchase -> order_completed, begin_checkout -> checkout_started, view_item -> product_viewed,
--                add_to_cart -> product_added, sign_up -> signed_up, login -> session_started; every other name
--                in snake_case (reportShared -> report_shared, page_view stays page_view)
--   event_at     TIMESTAMP_MICROS(event_timestamp), UTC
--   properties   a JSON object: every event_params key with its value (string_value, else int_value, else
--                double_value, else float_value), plus items (array of item_id, item_name, item_brand,
--                item_variant, item_category, price, quantity, item_revenue, coupon) when the event has items,
--                geo_country (geo.country, the country name GA4 derives from the IP) and device_category.
--
-- Schema reference: https://support.google.com/analytics/answer/7029846 (BigQuery Export schema).
-- Replace analytics_123456789 with your export dataset. To read a date range, add
--   WHERE _TABLE_SUFFIX BETWEEN '20260101' AND '20261231'
-- to the first CTE. batch_event_index exists in exports since 2024; for older tables, replace it with NULL.
WITH keyed AS (
  SELECT
    e.*,
    NULLIF(NULLIF(COALESCE(
      (SELECT COALESCE(p.value.string_value, CAST(p.value.int_value AS STRING)) FROM UNNEST(e.event_params) AS p WHERE p.key = 'transaction_id' LIMIT 1),
      e.ecommerce.transaction_id), ''), '(not set)') AS transaction_id,
    TO_HEX(MD5(CONCAT(
      COALESCE(e.user_pseudo_id, ''), '|', CAST(e.event_timestamp AS STRING), '|', e.event_name, '|',
      COALESCE(CAST(e.event_bundle_sequence_id AS STRING), ''), '|', COALESCE(CAST(e.batch_event_index AS STRING), '')))) AS row_hash
  FROM `analytics_123456789.events_*` AS e
),
events AS (
  SELECT
    k.*,
    CASE WHEN k.event_name = 'purchase' AND k.transaction_id IS NOT NULL THEN CONCAT('purchase:', k.transaction_id)
         ELSE CONCAT('ga4:', k.row_hash) END AS event_id
  FROM keyed AS k
),
deduped AS (
  SELECT *
  FROM events
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_timestamp, event_bundle_sequence_id, batch_event_index) = 1
),
pairs AS (
  SELECT
    d.event_id,
    p.key AS k,
    CASE
      WHEN p.value.string_value IS NOT NULL THEN TO_JSON_STRING(p.value.string_value)
      WHEN p.value.int_value IS NOT NULL THEN TO_JSON_STRING(p.value.int_value)
      WHEN p.value.double_value IS NOT NULL THEN TO_JSON_STRING(p.value.double_value)
      WHEN p.value.float_value IS NOT NULL THEN TO_JSON_STRING(p.value.float_value)
      ELSE 'null'
    END AS v
  FROM deduped AS d, UNNEST(d.event_params) AS p
  WHERE p.key NOT IN ('items', 'geo_country', 'device_category')
  UNION ALL
  SELECT
    d.event_id,
    'items' AS k,
    TO_JSON_STRING(ARRAY(
      SELECT AS STRUCT i.item_id, i.item_name, i.item_brand, i.item_variant, i.item_category, i.price, i.quantity,
             i.item_revenue, i.coupon
      FROM UNNEST(d.items) AS i WITH OFFSET AS pos
      ORDER BY pos)) AS v
  FROM deduped AS d
  WHERE ARRAY_LENGTH(d.items) > 0
  UNION ALL
  SELECT d.event_id, 'geo_country' AS k, TO_JSON_STRING(d.geo.country) AS v
  FROM deduped AS d
  WHERE d.geo.country IS NOT NULL AND d.geo.country <> ''
  UNION ALL
  SELECT d.event_id, 'device_category' AS k, TO_JSON_STRING(d.device.category) AS v
  FROM deduped AS d
  WHERE d.device.category IS NOT NULL AND d.device.category <> ''
),
props AS (
  SELECT event_id, CONCAT('{', STRING_AGG(CONCAT(TO_JSON_STRING(k), ':', v), ',' ORDER BY k), '}') AS properties
  FROM pairs
  GROUP BY event_id
)
SELECT
  d.event_id,
  NULLIF(d.user_id, '') AS user_id,
  NULLIF(d.user_pseudo_id, '') AS anonymous_id,
  CASE d.event_name
    WHEN 'purchase' THEN 'order_completed'
    WHEN 'begin_checkout' THEN 'checkout_started'
    WHEN 'view_item' THEN 'product_viewed'
    WHEN 'add_to_cart' THEN 'product_added'
    WHEN 'sign_up' THEN 'signed_up'
    WHEN 'login' THEN 'session_started'
    ELSE COALESCE(NULLIF(LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(d.event_name, r'([a-z0-9])([A-Z])', r'\1_\2'),
                                                   r'[^A-Za-z0-9]+', '_'), '_')), ''), 'unnamed_event')
  END AS event_name,
  TIMESTAMP_MICROS(d.event_timestamp) AS event_at,
  COALESCE(pr.properties, '{}') AS properties
FROM deduped AS d
LEFT JOIN props AS pr ON pr.event_id = d.event_id
