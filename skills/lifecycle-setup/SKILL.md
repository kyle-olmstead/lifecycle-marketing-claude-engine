---
name: lifecycle-setup
description: >
  Use when the user says "set up the lifecycle engine", "set up the LMM engine", "configure my
  stack", "what does the lifecycle engine see", "build my context file", "create a workspace", or on
  the first use of any other lifecycle-engine skill when no workspace (lmm.yaml) exists in the
  working folder. Inventories the connected platforms, records the messaging platform and the
  warehouse dialect, captures the brand, consent, sending and measurement facts the jobs need, and
  saves them in lmm.yaml, the workspace file every other skill and command reads.
metadata:
  version: "3.0.0"
---

# Lifecycle engine setup

Always read (once per conversation; skip it if you already read it in this conversation):
`${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`.

The goal is a workspace: `lmm.yaml` (platform, warehouse dialect, sending gate, frequency policy, offer policy,
measurement), `brand.yaml` (logo, colors, footer, voice), `data/`, `briefs/active/` and `runs/`. Every value is
labeled LIVE PULL (with the date), SUPPLIED or ESTIMATE; a blank means UNKNOWN. An older setup with
`lmm-engine/context.xml` keeps working; offer to carry its values into `lmm.yaml`.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py`.

## Step 1: Check the install

Run `lcm doctor`. If it fails on an import, install once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt` (Python 3.12 or later). If code cannot run here,
write `lmm.yaml` by hand from `${CLAUDE_PLUGIN_ROOT}/templates/workspace/lmm.yaml` and say in the summary that every
other skill will mark its artifacts "unchecked".

## Step 2: Create the workspace

`lcm init <folder> --name "<brand>" --platform <klaviyo|hubspot|braze|customerio|iterable|sfmc> --business-model
<ecommerce|subscription|saas|marketplace|services|sales_led_b2b>`.

## Step 3: Inventory what is connected

| Role | Connectors that fill it today | Recorded in `lmm.yaml` |
|---|---|---|
| messaging platform | Customer.io, Braze, Klaviyo, HubSpot (also Mailchimp, Brevo, Kit, OneSignal) | `platform` |
| warehouse | BigQuery, Snowflake, Neon (PostgreSQL), Blaze SQL | `warehouse_dialect`: bigquery, snowflake, postgres |
| product analytics | Mixpanel, Amplitude, Fullstory | none |
| enrichment | Clay | none |
| CRM | HubSpot, Salesforce | none |

Salesforce Marketing Cloud, Segment, RudderStack, Google Analytics 4 and Looker Studio had no Claude connector when
this plugin was published; ask the user to paste what a job needs. If Customer.io is connected, ask which Liquid
version the workspace runs (latest or legacy): the templates are written in a different dialect for each.

## Step 4: Pull before you ask

For each connected platform, read what you can and label it LIVE PULL with today's date: field lists and fill rates
for the fields flows will use, consent fields, recent send volume, the live flows with their entry volume. Exports go
in `data/` (`lcm import <source> <folder>` maps a vendor export to the canonical tables; `lcm check-data data` checks
them). Only then ask for what remains, at most three questions at a time, each with the default you will use:
transaction object, business model, buying unit (`measurement.randomization_unit`), postal address, sender identity,
recipient countries, caps and permitted send windows (the defaults in recipient local time: email 08:00 to 21:00,
SMS 09:00 to 20:00, push 09:00 to 21:00).

## Step 5: Write and check

Fill `lmm.yaml` and `brand.yaml` (`lcm brand check` validates the kit). Record the gate with `lcm deliverability
check <domain> --write-gate gate.json` and point `sending_gate` at it. Show a short table of what was found, what is
missing, and which skills are fully, partly or not yet usable; ask for a one-word confirmation before saving; then run
`lcm doctor` again and `lcm questions` to see which of the engine's questions the workspace now answers.

## Step 6: Recommend the first job

Name the program phase (PRE-BUILD, REMEDIATION, BUILD, STEADY STATE) and the one skill to run first. With no sending
gate, it is `deliverability-gate`.

## Rules

- Never store credentials, API keys or customer records in the workspace files; `lcm doctor` reports key names, never
  values.
- Never write to a connected platform during setup.
