# Architectural audit, v2.1 to v2.2

> **In plain English.** Version 2.2 was a review of the whole engine followed by a rebuild of the
> weak spots. Four things changed for the people using it. A campaign plan now saves its progress
> step by step, so a failure or a change no longer means starting over, and approved copy stays
> approved. Every plan is checked against the email and texting laws of the countries your customers
> live in, with the source for each rule. The engine now checks your sending domain live and removes
> people who have stopped engaging from marketing email, the two biggest levers on reaching the inbox.
> And one plain-English command runs everything, with a daily check that logs to Google Sheets and
> opens a ticket for each problem. Along the way, 37 defects were found and fixed (17 in the older
> tools, 20 caught by an independent review of the new code), each with a test so it cannot return.

October 2026. The brief asked for an audit of the whole repository against three bars: a
fault-tolerant multi-agent loop with real state, data and deliverability handling that cannot be
fooled by malformed input, and an interface a lifecycle manager can drive without code. This file
records what the audit found, what changed, and what was deliberately left alone. Every fix below
has a regression test that fails on v2.1.

The approach was to evolve the tested code rather than rewrite it. v2.1 already had typed contracts
between agents, SQL verified on four engines, and a simulator; a rewrite would have thrown that
evidence away.

## 1. Architecture and agents

| Finding in v2.1 | Change in v2.2 | Where |
|---|---|---|
| The orchestrator called the agents in one function. A failure anywhere lost all the work before it, and nothing recorded which step failed or why. | A stage loop (intake, analyst, strategist, architect, copywriter, experiment, QA, compliance) that saves every stage's output and an input hash to disk. A failed stage stops the run, the stages after it are marked skipped, and `orchestrator resume` restarts from the failed stage. A remote data load that fails with a network or server error is retried twice; a wrong path or bad input is not. Claude calls are retried by the Anthropic SDK, then fall back to offline drafts, and the plan says so. | `agents/pipeline.py`, `agents/stages.py` |
| No way to change one thing in a plan without regenerating all of it, so approved copy was rewritten by every edit. | `orchestrator revise --set touches=4` starts a new run linked to its parent. Copy is reused for every touch whose job, channel, audience and voice are unchanged (re-rendered and re-linted, never trusted blind), and an approval made with `orchestrator approve` carries over while its words do. | `agents/pipeline.py` |
| Strategic choices (which job each touch does, which channel, when, whether to offer anything) were buried in the architect and never explained. | A Strategist agent decides them and logs each decision with its reason. It drops a channel the data cannot support (SMS consent under 5%, push or sessions not tracked) instead of building touches that would all fall back to email. The plan opens with "Why the plan looks like this". | `agents/strategist.py` |
| QA checked mechanics, not law. A plan with no postal address passed. | A Compliance agent checks the plan against 22 rules in `config/compliance_rules.json` for the jurisdictions in the audience (from `users.country`, or the brief), each rule with its source. A failing rule blocks the plan. | `agents/compliance.py` |
| Behavioral triggers were designed, but nothing showed a marketer what they buy over the calendar newsletter. | `blast-vs-trigger` sorts each past blast's recipients into active, at risk, on time and late against the trigger's threshold, with the core-action rate of each band. | `agents/cohort_analyst.py` |
| Activation flows asked the event sender to add the holdout fields, and nothing did. | Opt-in real-time forwarding in the webhook parser: a qualifying event for an activation brief goes on to the ESP with `holdout_bucket` and `in_holdout` computed by the warehouse formula. Off unless `LCM_FORWARD=1`; never sends without credentials. | `scripts/webhook_parser.py` |

## 2. Data engineering and deliverability

### New capabilities

| Asked for | Delivered | Where |
|---|---|---|
| Automated SPF, DKIM, DMARC and MTA-STS checks | A live DNS collector (dnspython) that feeds the existing analyzer: every SPF include (for the 10-lookup count), DMARC with organizational fallback, MX, the MTA-STS TXT record and its HTTPS policy file, TLS-RPT, BIMI and DKIM selectors from each major sender's documentation. It refuses to report "no records" for a domain that does not resolve at all. | `tools/dns_collect.py` |
| Text-to-image validation | Visible characters per image, with template tags and hidden preheaders stripped, in every email lint. Image-only mail is an error; under 250 characters per image is a warning. | `tools/template_lint.py` |
| Automated suppression of unengaged cohorts | Engagement tiers from message events (clicks only; machine opens make opens meaningless), the unengaged share as a sending-gate input, and a reversible `lcm_sunset` attribute synced to Klaviyo, HubSpot, Braze or Customer.io. Nobody's consent is changed: a click brings them back. | `agents/engagement.py`, `tools/payloads.py` |
| Pydantic everywhere | Every tool input is validated at the edge by a Pydantic contract that names the field that is wrong, and the JSON Schemas in `schemas/` are generated from them (CI fails if they drift). The agents already passed typed models. | `tools/contracts.py` |

### Bugs fixed

| # | Bug | Effect | Test |
|---|---|---|---|
| 1 | Microsoft's Authentication-Results header has no authserv-id; the parser dropped its first result, and the auth state ignored the receiver's own DMARC verdict. | Outlook-delivered mail could read as failing DMARC. | `test_microsoft_authentication_results_without_authserv_id_are_read`, `test_auth_state_prefers_the_receivers_dmarc_verdict` |
| 2 | A subdomain sender covered by its parent's record was judged by `p=`, not `sp=`. | `p=reject; sp=none` read as protected; the DMARC step and BIMI eligibility were wrong for subdomains. | `test_subdomain_sender_is_judged_by_sp_not_p` |
| 3 | Payload values were coerced: `02134` became `2134`, long ids lost precision, HubSpot got Python's `True`. | Corrupted ZIP codes and ids in the ESP. | `test_leading_zeros_and_long_ids_stay_strings`, `test_hubspot_values_are_the_source_strings` |
| 4 | The Braze attribute pattern swallowed `${{ price }}` and `${% if %}`. | Valid templates failed lint, and broken ones could pass. | `test_braze_attribute_regex_leaves_liquid_after_a_dollar_sign` |
| 5 | One-click unsubscribe counted a DKIM signature covering both headers without checking it verified. | RFC 8058 compliance reported on a failing signature. | `test_one_click_unsubscribe_needs_a_passing_covering_signature` |
| 6 | DuckDB divides integers to a float; PostgreSQL truncates. | An integer-division bug in the reference SQL would pass the fixtures and fail in the warehouse. | `test_duckdb_divides_integers_like_postgres` |
| 7 | The LTV query's `share_with_revenue` was always 1 (everyone has revenue in the period of their first order). | A meaningless column. Replaced by `repeat_share`, revenue after the first order's period, in all three dialects. | `test_ltv_repeat_share_is_not_trivially_one` |
| 8 | Request files were written indented but sized compact. | A batch sized under 5 MB could be over it as posted. | `test_written_request_files_are_the_size_that_was_checked` |
| 9 | SMS segments were `ceil(units / 153)`, which splits a two-septet character or an emoji across segments. | Undercounted segments, so undercounted cost. | `test_sms_escape_characters_never_split_across_segments` |
| 10 | SPF: an include loop counted as valid; unknown modifiers (`ra=`) flagged as errors; `redirect=` counted when `all` makes it ignored. | Wrong lookup counts and false alarms. | `test_spf_include_loop_is_a_permerror_and_modifiers_are_ignored`, `test_spf_redirect_is_ignored_when_all_is_present` |
| 11 | CSV reading kept an Excel byte-order mark in the first column name and split quoted fields on newlines. | The id column was not found; rows were mangled. | `test_read_rows_handles_bom_and_newlines_inside_quotes` |
| 12 | HubL `unless` failed to parse; AMPscript read `@next` as the keyword NEXT. | Valid templates failed lint. | `test_hubl_unless_and_ampscript_keyword_variables` |
| 13 | `v=dmarc1` was accepted. | RFC 7489 requires `v=DMARC1` exactly; receivers ignore the record. | `test_dmarc_version_tag_is_case_sensitive` |
| 14 | MTA-STS `mode: none` still checked MX coverage. | False critical findings on a policy that is switched off. | `test_mta_sts_mode_none_skips_mx_coverage` |
| 15 | Readouts always said "95%" whatever alpha was, and suggested a 25% holdout to flows already at 25%. | Wrong labels and a pointless suggestion. | `test_interval_label_follows_alpha`, `test_no_larger_holdout_suggested_when_already_at_25_percent` |
| 16 | The SMS opt-out check matched "stop by our store". | A message with no opt-out instruction passed. | `test_sms_opt_out_needs_an_instruction_not_the_word` |
| 17 | `warmup.plan(days=0)` crashed; zero engaged capacity still scheduled sends. | A ladder for nobody. | `test_zero_days_is_rejected_and_zero_engaged_capacity_schedules_nothing` |

### Facts re-checked

Compliance rules, ESP limits, DKIM selectors and GitHub Action versions were re-read from primary
sources on 2026-10-20. Changes that followed:

- State texting laws: Connecticut (9 a.m. to 8 p.m., texts covered) is now the earliest start, and
  Oregon (8 p.m. end and 3 per 24 hours, texts covered from 1 January 2026) joins Florida, Oklahoma and
  Maryland. The plan's SMS send hour must fall inside 9 a.m. to 8 p.m. local, and a send before noon
  is flagged for Sundays (Texas). Whether the Florida, Maryland, Oklahoma and Texas hour limits reach
  texts is not settled; the rules file says so, and they are applied anyway. These are the states the
  file tracks, not all fifty.
- New rules: bulk-sender authentication (Gmail, Yahoo, Microsoft), CAN-SPAM ad identification, SMS
  opt-out retention (Virginia: 10 years), and Sunday texting (some states restrict Sunday solicitation
  further or ban it; a question for counsel).
- Customer.io Pipelines batch requests are limited to 500 KB per request and 32 KB per call (the batch
  endpoint's own page); the engine packs to 450,000 and 30,000 bytes.
- DKIM selectors: the old default list guessed (`cio`, `kl`, `pm`, `smtp`); the new one has only
  selectors each provider documents, and says which providers need `--selectors`.
- GitHub Actions: `checkout` and `setup-python` v7, `upload-artifact` v7, Google auth and gcloud v3
  (all Node 24; the v4 and v5 actions ran on the deprecated Node 20).

## 3. Marketer-first usability and automation

| Finding in v2.1 | Change in v2.2 | Where |
|---|---|---|
| Eight entry points with their own flags. | `lcm`: one command that takes a sentence or a YAML job file, picks the job by rules (the same sentence always runs the same job), fills in what it can, and asks for what it cannot. `--dry-run` shows the job without running it. | `agents/cli.py`, `pyproject.toml` |
| A brief from one sentence had no way to set a field without editing YAML. | `--set FIELD=VALUE` on `plan` and `brief`, the same syntax as `revise`. | `agents/orchestrator.py` |
| The daily workflow audited one brief and wrote two tabs. | `scripts/daily.py` audits every brief in `briefs/active/`, rebuilds the engagement tiers, checks each domain in `LCM_DOMAINS`, logs six tabs to Sheets in one sync, and writes one GitHub issue per finding. One step failing does not stop the others; the workflow fails at the end if any did. | `scripts/daily.py`, `.github/workflows/daily_cohort_audit.yml` |
| In BigQuery mode the per-person tier list would have come back through `bq`. | The warehouse returns six aggregate rows (`engagement.counts_sql`). | `agents/engagement.py` |

## 4. Deliberately not done

- **No language model in the router.** A rule-based router is predictable and testable; when it is
  unsure it asks. Claude still writes the copy, where judgment pays.
- **Suppression is an attribute, not an unsubscribe.** Klaviyo's suppression API marks profiles
  USER_SUPPRESSED; that is reversible only through a second call, and an unsubscribe is not reversible
  at all. A sunset is a business decision, so it stays a segment exclusion the marketer controls.
- **The daily job never writes to an ESP.** It reports and builds request files; a person decides to
  send them.
- **No legal sign-off.** The Compliance agent is a rules engine with sources. Anything it marks warn
  is for counsel.
- **The win-back trigger stays a warehouse job.** An absence cannot be seen in an event stream, so
  real-time forwarding is for activation only.

## 5. Verification

- 303 tests (v2.1 had 206), including PostgreSQL 16 for the PostgreSQL SQL, run in CI with
  `tools/repo_check.py` and smoke runs of `lcm`, `revise`, `status`, the daily job and the schema
  generator.
- Every number in the worked example (`examples/teammate/ridgeview_winback_14d/`) is regenerated by CI
  and compared.
- An independent reviewer, working only from the diff, reproduced 20 defects in the first v2.2 build.
  All are fixed, each with a regression test. The ones that mattered most:
  - a `users.country` column that was empty made the Compliance agent apply no country's rules (now
    United States rules are assumed, with a warning, as when there is no data);
  - the one-click unsubscribe check matched receiver verdicts to signatures by domain only, so a
    second signature's pass could stand in for a failing one (now matched by selector and signature);
  - the SMS opt-out check accepted "NON-STOP savings" and "end to end", so a text could ship without
    STOP language (now it needs an instruction, such as "Reply STOP");
  - a resumed run could reuse its own stale estimates, lose its parent's copy, or keep an approval on
    rewritten words (stages now read only the stages before them, a revision reloads its parent, and
    an approval stands only while its words do);
  - copy was reused when the audience changed (the copy key now includes the flow type, the events
    and the threshold);
  - two `_dmarc` records were reported as one valid policy (receivers apply none; now critical);
  - Customer.io's batch limit is 500 KB, not 1 MB (batches now pack to 450,000 bytes);
  - remote data loads were never retried, and the docs said they were (a server error is now
    retried twice).
