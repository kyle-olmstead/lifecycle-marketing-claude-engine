# Importing your data

> **In plain English.** The engine reads one set of tables (people, events, orders, consent, email activity). Your
> tools do not export those tables; they export their own files. `lcm import` turns the files you can download from
> Shopify and Klaviyo into the engine's tables, tells you every guess it had to make, and then runs `lcm check-data`,
> which names any column that would break or quietly mislead the engine. The goal: value on your own data in an hour,
> with no engineer building CSVs by hand.

## The one-hour path

1. **Download the exports** (where each one lives is below): from Shopify the customers, orders and abandoned
   checkouts exports; from Klaviyo a profile export of everyone, and the event export.
2. **Put them in one folder**, for example `exports/shopify/` and `exports/klaviyo/`. File names do not matter: each
   mapper recognizes its files by their column headers.
3. **Import:**

   ```bash
   lcm import auto exports/ --out data/
   ```

   `auto` finds every export it recognizes and combines them. Name a source instead (`lcm import shopify
   exports/shopify --out data/`) to import one tool only. Until `lcm` has the subcommand wired, run the same thing as
   `python3 adapters/sources/cli.py auto exports/ --out data/`; `--list` shows the sources it knows.
4. **Read the report it prints.** For each source: the tables and rows it wrote, the **assumptions** it made (anything
   marked ASSUMED is a value your export did not carry, and the report says which conservative choice was made),
   **warnings**, and the columns it did not use. The same report is saved as `data/import_report.json`, next to the
   data, so the assumptions travel with it.
5. **Read the check.** The import ends with `lcm check-data data/`. PASS means the engine can read the folder; any
   error is listed with the column, the number of rows, a few bad values and a one-line fix.
6. **Point the engine at `data/`**: pass `--data data/`, or keep the folder as the `data/` of your workspace.

Run `lcm check-data <folder>` on its own whenever the data changes, or on tables your warehouse team builds directly
(`python3 engine/data/check.py <folder>` until it is wired; add `--json` for a machine-readable report).

## What check-data checks

Every check names the table and column. Errors stop the engine or make it read the data wrongly (exit code 1);
warnings mean some rows are skipped or misread (exit code 0); notes are for information.

| Check | Severity | What it catches |
|---|---|---|
| Required tables and columns | error | `users` and `track_events` missing, or a required column such as `orders.status` missing |
| Types | error | values that do not parse as the column's type: timestamps, numbers (`$17.00`), whole numbers, true or false, JSON objects in `track_events.properties` |
| UTC offsets | warning | timestamps such as `2026-03-15 14:22:10+02:00`: the loader keeps the clock time and drops the offset |
| Unique ids | error | duplicate `users.user_id`, `track_events.event_id`, `orders.order_id`, `invoices.invoice_id`, `subscriptions.subscription_id`, `refunds.refund_id` |
| References | error at 5 percent or more, else warning | `track_events.user_id` or `orders.user_id` not in `users`, `line_items.order_id` not in `orders`, invoices pointing at unknown subscriptions |
| Event names | warning | names that are not snake_case object_action (`Order Completed`, `orderCompleted`); the fix suggests `order_completed` |
| Emails | warning | malformed addresses, or capital letters (SQL joins on email are case-sensitive) |
| Consent in the future | error | a consent dated after today would outrank every real later unsubscribe |
| Time zones, countries, currencies, phones | warning | `users.timezone` not an IANA name (America/Chicago), country not ISO 3166-1 alpha-2, currency not three capital letters, phone not E.164 |
| Status values | warning | values the engine does not recognize: an order with status `paid` is not counted as revenue (it expects `completed`) |
| Events in the future, freshness | warning | rows dated after today, and the newest event more than 7 days old |
| Unknown columns, other files | note | columns and files the engine ignores, with a suggestion when the name is close to a canonical one |

## How several sources are combined

When `auto` imports more than one tool, the sources run in a fixed order, commerce first (Shopify, then Stripe,
HubSpot, Klaviyo, Customer.io, GA4, RudderStack, Segment), and the earlier source wins a conflict:

* **People are matched by email** (lower-cased). A later source's ids are replaced by the earlier source's id
  everywhere, so a Klaviyo open and a Shopify order belong to the same person.
* **Each person keeps the first non-blank value** of each column, and the earliest sign-up date that a source actually
  read (a date a mapper had to assume never replaces a real one).
* **An order or checkout reported by two tools is kept once**: Klaviyo's Placed Order for a Shopify order is dropped in
  favor of the Shopify row (same order id), and the same for checkouts.
* **Consent is never made more permissive by a guess.** An undated "yes" from one tool is dropped when another tool has
  dated consent records for that person; an undated "no" is always kept, dated at the end of the export so it outranks
  earlier grants. The newest record wins: a later opt-in lifts an earlier unsubscribe.

## Shopify

**Which exports, and where** (all from the Shopify admin, as CSV):

| Export | Where | Notes |
|---|---|---|
| Customers | Customers > Export > All customers > CSV | One row per customer: email, marketing consent yes or no, default address, phone. [Shopify help](https://help.shopify.com/en/manual/customers/import-export-customers) |
| Orders | Orders > Export > choose the orders (for example Orders by date) > Export orders | One row per line item. Exports of more than 50 orders are emailed to you. [Shopify help](https://help.shopify.com/en/manual/orders/export-orders) |
| Abandoned checkouts (optional) | Orders > Abandoned checkouts > Export | Emailed as CSV. Shopify's help center does not document its columns; the mapper reads the ones it shares with the orders export. |

**Command:** `lcm import shopify exports/shopify --out data/`

**What maps to what:**

| Shopify | Engine table | How |
|---|---|---|
| Customers export | `users` | email (lower-cased), country from Default Address Country Code, phone in E.164. The id is Customer ID when your export has one, otherwise a stable code made from the email (`em_...`), the same code the Klaviyo mapper makes, so the two join |
| Orders export, first row of each order | `orders` | Id (or Name), Created at converted to UTC, status from Financial Status and Canceled at (paid and partially refunded are `completed`, refunded is `refunded`, voided or canceled is `canceled`), gross = Subtotal + Discount Amount, net = Subtotal - Refunded Amount (shipping and taxes left out) |
| Orders export, every row | `line_items`, `products` | quantity, Lineitem price, Lineitem SKU as the product id, Lineitem name |
| Discount Code, Discount Amount | `discounts` | one row per code |
| Refunded Amount | `refunds` | one row per refunded order |
| Accepts Marketing (per order), Accepts Email Marketing (per customer) | `consent_events`, `users.email_consent_at` | see below |
| Each order; each abandoned checkout | `track_events` | `order_completed` with order id, net amount, currency and items; `checkout_started` with checkout id, value and items |

**What the mapper cannot know, and what it does instead:**

* **When someone opted in.** The customers export says yes or no, never when. Where an order shows Accepts Marketing
  = yes, consent is dated at that order. Where there is no such order, consent is dated at the customer's first known
  activity and marked `import_undated` (ASSUMED): dated early on purpose, so a real unsubscribe from Klaviyo outranks
  it. A customer who said yes on an order but no today gets a revoke dated at the end of the export.
* **SMS consent.** Accepts SMS Marketing has no date, and SMS needs a dated consent record (TCPA, 10DLC), so an undated
  SMS yes is not treated as consent. Import Klaviyo's SMS consent, which has timestamps, to enable SMS.
* **When a refund happened.** The export has the refunded amount, not the date: it is dated at Canceled at, else at the
  order time (ASSUMED), so revenue is never counted in a period after its refund.
* **Sign-up dates.** The customers export has no dates. A customer's sign-up date is their first order or checkout; a
  customer with neither gets the start of the export window (ASSUMED), so nobody looks newly signed up. Klaviyo's
  profile creation date replaces it when both are imported.
* **Discount type, product category, product links.** Not in these exports. Klaviyo's product views fill in category
  and link when both are imported.
* **Checkouts that were not abandoned.** The abandoned checkouts export holds only abandoned ones; Klaviyo's Checkout
  Started covers the rest.
* **Names and street addresses** are deliberately not imported: the engine needs neither.

## Klaviyo

**Which exports, and where:**

| Export | Where | Notes |
|---|---|---|
| Profiles | Audience > Lists & segments > open a segment that holds everyone > Manage segment > Export segment to CSV. The file appears under Settings > Other > Downloads | Include the consent fields (Email Marketing Consent and its timestamp, SMS Marketing Consent and its timestamp), country, time zone, the created date and the profile id (Klaviyo ID), which links events to people. Owner, Admin or Analyst role. [Klaviyo help](https://help.klaviyo.com/hc/en-us/articles/115005078687) |
| Events | SFTP data transfer, Data export tab > Create export > Events (CSV or JSON lines, gzip-compressed) | The list export cannot include events, so they come from the SFTP export: columns id, metric_id, profile_id, event_properties, datetime, uuid. Owner, Admin or Manager role. [Klaviyo developers](https://developers.klaviyo.com/en/docs/export_event_data_sftp) |
| Metric names | the Get Metrics API response saved as `metrics.json`, or a two-column CSV `metric_id,name` | The SFTP export names each metric by id only; this file turns ids into names such as Placed Order. Get Events API output (with `include=metric`) is also read as is. |

**Command:** `lcm import klaviyo exports/klaviyo --out data/`

**What maps to what:**

| Klaviyo metric | Engine table | Event |
|---|---|---|
| Placed Order | `track_events` | `order_completed` (order id, value, items) |
| Checkout Started (Shopify) or Started Checkout (custom) | `track_events` | `checkout_started` |
| Viewed Product | `track_events` | `product_viewed`; also fills `products` with name, category and link |
| Added to Cart | `track_events` | `product_added` |
| Active on Site | `track_events` | `session_started` |
| Received Email | `message_events` | `delivered` |
| Opened Email | `message_events` | `opened`, with `machine_open` from Klaviyo's Apple Privacy Open flag ([Klaviyo help](https://help.klaviyo.com/hc/en-us/articles/4416791883163)) |
| Clicked Email | `message_events` | `clicked` |
| Bounced Email | `message_events` | `bounced` |
| Marked Email as Spam | `message_events`, `consent_events` | `complained`, and email consent revoked |
| Unsubscribed | `message_events`, `consent_events` | `unsubscribed`, and email consent revoked |
| Subscribed to List | `consent_events` | email consent granted at that moment |
| Profile export consent columns | `consent_events`, `users` | Subscribed with its timestamp is a dated grant; Unsubscribed is a revoke |

Metric names follow Klaviyo's own lists ([email metrics](https://help.klaviyo.com/hc/en-us/articles/360036974872),
[Shopify and onsite metrics](https://help.klaviyo.com/hc/en-us/articles/115005080447)). Anything else, such as
Fulfilled Order, is skipped and counted in the report.

**What the mapper cannot know, and what it does instead:**

* **Consent without a timestamp.** A Subscribed profile with no timestamp is dated at the profile's creation and marked
  `import_undated` (ASSUMED); an Unsubscribed or Suppressed one with no timestamp is dated at the end of the export.
  An SMS Subscribed with no timestamp is not treated as SMS consent.
* **Which status the timestamp belongs to.** The consent timestamp is read as the moment the current status was set.
* **Time zones of timestamps.** Profile export dates carry no offset and are read as UTC (ASSUMED, counted in the
  report).
* **Suppressed** (a hard bounce or spam suppression) is read as a revoke: the address cannot be emailed either way.
* **Revenue.** Klaviyo's `$value` depends on how your store sends it; when Shopify is imported too, Shopify's orders
  are the revenue of record and Klaviyo's duplicate orders are dropped.
* **Product ids.** Klaviyo's product id is matched to the SKU through orders that carry both, so product views join
  the Shopify line items.

## Stripe

See the mapper's docstring.

## HubSpot

See the mapper's docstring.

## Customer.io

See the mapper's docstring.

## GA4

See the mapper's docstring.

## RudderStack

See the mapper's docstring.

## Segment

See the mapper's docstring.

## Try it on the demo brand

`examples/brands/northfield_dtc/` is a fictional coffee roaster with synthetic Shopify and Klaviyo exports in their
real formats (`exports/`, written by `make_exports.py`) and the result of importing them (`data/`):

```bash
python3 adapters/sources/cli.py auto examples/brands/northfield_dtc/exports --out /tmp/northfield
python3 engine/data/check.py /tmp/northfield
```

The saved report starts like this (`data/import_report.json`):

```json
{
  "sources": [
    {"source": "shopify", "tables": {"users": 2000, "orders": 1332}, "assumptions": ["ASSUMED: ..."]},
    {"source": "klaviyo", "tables": {"users": 2000, "message_events": 11738}, "assumptions": ["..."]}
  ],
  "merge": ["klaviyo: 2,000 user(s) already known, 0 new."]
}
```

## Adding a source

A mapper is one file, `adapters/sources/<name>.py`, that defines `NAME`, `DESCRIPTION`, `detect(folder)` (true when the
folder holds its files) and `map(folder, out_dir)`, which writes canonical `<table>.csv` files and returns a
`MappingReport` (`adapters/sources/report.py`): tables and rows written, columns not used, and every assumption in plain
words. `adapters/sources/registry.py` finds it automatically. The shared helpers in `adapters/sources/export_io.py`
read CSVs (plain or gzip), match columns case-insensitively, parse vendor dates into UTC, and write tables in one
deterministic shape.
