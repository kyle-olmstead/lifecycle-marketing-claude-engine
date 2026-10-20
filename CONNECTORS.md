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

1. **Paste the evidence.** Headers, `dig` output, a CSV export, query results. The engine labels it
   SUPPLIED; a gate cannot read GREEN on authentication evidence nobody observed, so include the date
   you ran the lookups.
2. **Go through the warehouse.** Most CDPs (Segment, RudderStack) and ESPs can sync to BigQuery or
   Snowflake, which have connectors. Point the engine at those tables.
3. **Add a custom connector.** Your Claude admin can add a custom MCP connector for the tool's API;
   setup detects custom connectors the same way it detects directory ones.

## MCP servers and event ingest (2.1)

- `config/mcp_server_config.json` lists the vendor-run MCP servers checked on 2026-10-20: local
  servers for Klaviyo and BigQuery (MCP Toolbox) you add to Claude Desktop's config, and remote ones
  for HubSpot, Klaviyo, Braze, Customer.io, BigQuery, Snowflake and Google Sheets that you add as
  custom connectors. Snowflake-Labs/mcp and Braze's earlier local server are deprecated by their
  vendors; the file points to the replacements.
- It also carries the teammate's own local server (`scripts/mcp_server.py`), for Claude Desktop
  without the plugin.
- Segment and GA4 have no connector: send their events to `scripts/webhook_parser.py`, which writes
  the canonical `track_events` rows the cohort queries read, or use GA4's BigQuery export.

## Safety

- Read before ask: with a connector, the engine pulls before it asks you, and labels what it read.
- Write only with approval of the specific change. New automations are created disabled.
- Credentials never appear in prompts, context files or this repository.
