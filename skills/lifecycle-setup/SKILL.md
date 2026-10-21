---
name: lifecycle-setup
description: >
  Use when the user says "set up the lifecycle engine", "set up the LMM engine", "configure my
  stack", "what does the lifecycle engine see", "build my context file", or on the first use of any
  other lifecycle-engine skill when no context file exists in the working folder. Inventories the
  connected platforms, records each one's template and SQL dialect, captures the brand, consent,
  deliverability and benchmark facts the modules need, and saves them as the context file every
  other skill reads.
metadata:
  version: "2.0.0"
---

# Lifecycle engine setup

Produce `lmm-engine/context.xml` in the user's working folder, shaped like
`${CLAUDE_PLUGIN_ROOT}/templates/context/context_loader.xml`, with every value labeled LIVE PULL (with
`observed_at`), SUPPLIED or ESTIMATE. Every other skill reads this file before it does anything.

## Step 1: Read the operating rules

Read `${CLAUDE_PLUGIN_ROOT}/prompts/core/system_prompt.md` once per conversation. Its operating principles
and syntax rules bind every skill in this plugin.

Then check that the repository tools can run: `python3 ${CLAUDE_PLUGIN_ROOT}/engine/repo_check.py`.
If it fails on an import, install the dependencies once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt` (Python 3.12 or later). If code
cannot run in this environment, set `<environment><tools_available>` to `false` in the context file (`true` when the check passes) so every other
skill marks its artifacts "unchecked" instead of verified.

## Step 2: Inventory what is connected

List the connectors present in this Claude workspace and map each to a role:

| Role | Connectors that fill it today | Dialect to record |
|---|---|---|
| messaging platform | Customer.io, Braze, Klaviyo, HubSpot (also Mailchimp, Brevo, Kit, OneSignal) | customerio or customerio_legacy, braze, klaviyo, hubl |
| warehouse | BigQuery, Snowflake, Neon (PostgreSQL), Blaze SQL | bigquery, snowflake, postgresql |
| product analytics | Mixpanel, Amplitude, Fullstory | none |
| enrichment | Clay | none |
| CRM | HubSpot, Salesforce | none |

Salesforce Marketing Cloud, Segment, RudderStack, Google Analytics 4 and Looker Studio had no Claude
connector when this plugin was published; for those, ask the user to paste what the module needs.
Ask which Customer.io Liquid version the workspace runs (latest or legacy) if Customer.io is
connected; it changes how fallbacks are written.

## Step 3: Pull before you ask

For each connected platform, read what you can and label it LIVE PULL with today's date: field
lists and fill rates for the fields flows will use, consent fields, recent send volume, the live
flows with their entry volume. Only then ask the user for what remains, at most three questions at a
time, each with the default you will use: transaction object, business model, buying unit, postal
address, sender identities, recipient geography, quiet hours and caps.

## Step 4: Write the context file

Fill the template's sections (organization, voice_module, stack, data, deliverability, benchmarks,
program, constraints). Leave unknowns empty; empty means UNKNOWN. Show the user a short summary
table of what was found, what is missing, and which skills are fully, partly or not yet usable, and
ask for a one-word confirmation before saving.

## Step 5: Recommend the first job

Name the program phase you see (PRE-BUILD, REMEDIATION, BUILD, STEADY STATE) and the one skill to run
first. If no sending gate exists, it is `deliverability-gate`.

## Rules

- Never store credentials, API keys or customer records in the context file.
- Never write to a connected platform during setup.
