# Connectors

> **In plain English.** The engine can read a platform directly only when that platform has a
> connector in your Claude workspace. Everything else still works: you paste the evidence (headers,
> `dig` output, query results, exports) and the engine labels it SUPPLIED instead of LIVE PULL.

*Checked against Claude's connector directory on 2026-10-19, from one organization's view. The
directory changes often, organizations can add custom connectors, and availability can differ by
plan. Run `lifecycle-setup` for the truth about your workspace.*

## By category

| Category | Connector available | No connector found | What the engine reads | What it may write (only with your approval) |
|---|---|---|---|---|
| Messaging and ESP | HubSpot, Klaviyo, Braze, Customer.io; also Mailchimp, Brevo, Kit, Resend, OneSignal, Pushwoosh | Salesforce Marketing Cloud | campaign and flow performance, profiles and properties, segments, templates | drafts, templates and properties; never a live send without explicit approval |
| Warehouse | BigQuery, Snowflake (Cortex-based), Neon (PostgreSQL), Blaze SQL (any SQL database) | Redshift | schemas, fill rates, query results | nothing; the engine runs read queries only |
| CDP | none found | Segment, RudderStack | n/a | n/a |
| Product analytics | Mixpanel, Amplitude, Fullstory | Google Analytics 4 (available only through third-party connectors such as Supermetrics), Heap | event lists, property fill, funnels, experiment results | nothing |
| BI | Omni Analytics, Polar Analytics | Looker Studio | saved reports | nothing |
| Enrichment | Clay, Vibe Prospecting, Leadfeeder | | contact and company enrichment | enrichment requests with approval |
| CRM | HubSpot, Salesforce | | contacts, companies, deals, invoices, properties | properties and tasks, with approval |
| SMS | Twilio (documentation search only) | carrier and 10DLC consoles | n/a | n/a |

## Template dialects by platform

| Platform | Dialect the engine writes | Linter dialect |
|---|---|---|
| Customer.io | Liquid (latest or legacy) | `customerio`, `customerio_legacy` |
| Braze | Liquid with Braze attribute syntax | `braze` |
| Klaviyo | Django-style template language | `klaviyo` |
| HubSpot | HubL | `hubl` |
| Salesforce Marketing Cloud | AMPscript | `ampscript` |

## When your tool has no connector

1. **Import the export.** `lcm import` maps a vendor's own export into the engine's tables: Shopify (customers,
   orders, abandoned checkouts), Klaviyo (profiles and the event export), Stripe (customers, subscriptions,
   invoices), HubSpot (contacts), Customer.io (people and deliveries), GA4 (the BigQuery export), Segment and
   RudderStack (their warehouse tables). `lcm import auto <folder> --out data` detects each one, merges several by
   email, and runs `lcm check-data` on the result. Mapping rules and what each vendor leaves out:
   [`docs/IMPORTING_DATA.md`](docs/IMPORTING_DATA.md).
2. **Paste the evidence.** Headers, `dig` output, a CSV export, query results. The engine labels it
   SUPPLIED; a gate cannot read GREEN on authentication evidence nobody observed, so include the date
   you ran the lookups.
3. **Go through the warehouse.** Most CDPs (Segment, RudderStack) and ESPs can sync to BigQuery or
   Snowflake, which have connectors. Point the engine at those tables.
4. **Add a custom connector.** Your Claude admin can add a custom MCP connector for the tool's API;
   setup detects custom connectors the same way it detects directory ones.

## MCP servers and event ingest

- `config/mcp_server_config.json` lists the vendor-run MCP servers checked on 2026-10-20: local
  servers for Klaviyo and BigQuery (MCP Toolbox) you add to Claude Desktop's config, and remote ones
  for HubSpot, Klaviyo, Braze, Customer.io, BigQuery, Snowflake and Google Sheets that you add as
  custom connectors. Snowflake-Labs/mcp and Braze's earlier local server are deprecated by their
  vendors; the file points to the replacements.
- It also carries the teammate's own local server (`engine/mcp_server.py`), for Claude Desktop
  without the plugin.
- Segment and GA4 have no connector: send their events to `adapters/sources/webhooks.py`, which writes
  the canonical `track_events` rows the cohort queries read, or use GA4's BigQuery export.

## What the engine builds for each ESP

Nothing below is sent by the engine on its own. The tools build complete requests, with
`${PLACEHOLDER}` names where credentials go, and you (or a job you run) send them.

| Request | Klaviyo | HubSpot | Braze | Customer.io |
|---|---|---|---|---|
| Trigger event (`adapters/destinations/event_payloads.py`) | `POST /api/events`, revision 2026-10-15 | `POST /events/2026-09/send/batch`, 500 per request | `POST /users/track`, 75 objects per request | Pipelines `POST /v1/track` |
| Person attributes (`adapters/destinations/payloads.py attribute_sync`, the engagement tier and sunset flag) | `POST /api/profile-bulk-import-jobs`, 10,000 profiles and 5 MB per job | `POST /crm/objects/2026-09/contacts/batch/upsert`, 100 per request, values as strings | `POST /users/track` `attributes`, 75 per request, `_update_existing_only: true` | Pipelines `POST /v1/batch` of `identify` calls, packed to 450,000 bytes (the limit is 500 KB, 32 KB per call) |

HubSpot's changelog lists the 2026-09 date-versioned APIs as released on 8 September 2026, while its
versioning page still called 2026-03 the latest when this was checked; if a 2026-09 path is refused,
use 2026-03. Every endpoint, limit and source link is in `config/esp_payload_schemas.json`.

**Real-time triggers.** With `LCM_FORWARD=1`, the webhook parser forwards each activation brief's
qualifying event to its ESP as it arrives, with the holdout assignment added. Credentials come from
the environment: `KLAVIYO_PRIVATE_KEY`; `HUBSPOT_PRIVATE_APP_TOKEN` and `HUBSPOT_HUB_ID`;
`BRAZE_REST_API_KEY` and `BRAZE_INSTANCE`; `CUSTOMERIO_PIPELINES_API_KEY_BASE64` and, for EU
workspaces, `CUSTOMERIO_EU=1`. Salesforce Marketing Cloud entry is a Journey Builder API Event, which
the parser does not send.

**Live DNS.** `engine/deliverability/dns_collect.py` (and `lcm "check deliverability for <domain>"`) reads your
sending domain's records with the system resolver. No connector is needed; the lookups are public.

## Safety

- Read before ask: with a connector, the engine pulls before it asks you, and labels what it read.
- Write only with approval of the specific change. New automations are created disabled.
- Credentials never appear in prompts, context files or this repository.
