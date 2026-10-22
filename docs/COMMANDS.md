# Commands

Every job the engine does is one `lcm` command, and every command is the same tool you could run directly as a Python
module. `lcm COMMAND --help` lists each command's options; every option says what it takes and what it defaults to.
`lcm "a sentence"` routes plain English to the same commands (rules, not a model: the same sentence always runs the same
job, and it asks for what is missing instead of guessing).

Inside a workspace (`lmm.yaml` in the current folder or above it, or the folder named by `LCM_WORKSPACE`), every command
uses that workspace's platform, brand, sending gate, policies and data. Outside one, commands use the bundled demo
workspace and say so.

<!-- generated: commands -->
**Set up**

| Command | What it does |
|---|---|
| `lcm init` | create a workspace (lmm.yaml, brand.yaml, data/, briefs/active/, runs/) |
| `lcm doctor` | check the install, workspace, brand kit, data and key names |
| `lcm check-data` | check exports against the canonical tables: missing, late, empty, malformed |
| `lcm import` | map a vendor export (Shopify, Klaviyo, Stripe, HubSpot, GA4, Segment...) to canonical tables |
| `lcm questions` | the questions the engine asks before planning, and which ones your workspace answers |

**Plan**

| Command | What it does |
|---|---|
| `lcm plan` | one brief in, a build-ready plan out |
| `lcm brief` | turn a sentence into an editable YAML brief |
| `lcm revise` | a new run from an old one with fields changed; approved copy is kept |
| `lcm approve` | approve a copy variant so revisions keep it |
| `lcm status` | a run's stages |
| `lcm resume` | continue a run that failed part way |
| `lcm simulate` | dry-run one flow spec, or every live flow at once with --program |

**Build and send**

| Command | What it does |
|---|---|
| `lcm render` | compile the MJML email templates with the brand kit |
| `lcm brand` | validate a brand kit and print its contrast table (lcm brand check) |
| `lcm lint` | lint a template in its platform dialect |
| `lcm sms` | SMS encoding, segments and opt-out language |
| `lcm sync` | profile sync request bodies for the platform (tiers, sunset flags) |
| `lcm events` | push the flow's trigger events from the warehouse ledger |
| `lcm build` | the ESP build pack for a plan: native trigger, steps, splits, capability checks, importable artifacts |
| `lcm send` | send requests safely: a dry run unless --approve, then batched, idempotent, retried and receipted |
| `lcm sheets` | log plans and the morning check to Google Sheets |

**Deliverability**

| Command | What it does |
|---|---|
| `lcm deliverability` | check, analyze, gate, monitor, bounces, complaints, providers |
| `lcm warmup` | a warm-up schedule for a new domain or IP, with abort rules |
| `lcm engagement` | engagement tiers and the sunset list, by clicks not opens |

**Measure**

| Command | What it does |
|---|---|
| `lcm size` | people and months a holdout needs |
| `lcm readout` | read a holdout: incremental, not attributed |
| `lcm experiments` | the experiment registry: add, list, check overlaps, record looks |
| `lcm sequential` | group-sequential and always-valid stopping boundaries |
| `lcm cluster` | ICC, design effect and cluster-aware sizing for account-level tests |
| `lcm global-holdout` | the program-wide holdout every flow excludes |
| `lcm discounts` | discount dependency by customer and flow |
| `lcm roi` | what the program is worth next year |

**Analyze**

| Command | What it does |
|---|---|
| `lcm audit` | audit a flow's cohort against the data |
| `lcm blast` | compare a newsletter or blast with a triggered flow |
| `lcm priorities` | the next flows worth building, ranked by money |
| `lcm tracking` | are the events firing: schema drift, nulls, late arrivals |
| `lcm retention` | retention curves and the churn cliff |
| `lcm daily` | the morning check: what broke overnight |
| `lcm workflow` | run a named graph of commands: morning_check, new_flow, quarterly_review, or your own |
| `lcm webhooks` | parse or receive platform webhooks |

**Developer**

| Command | What it does |
|---|---|
| `lcm mcp` | run the MCP server (stdio) |
| `lcm sql-check` | parse and check generated SQL in every dialect |
| `lcm repo-check` | the repository's own consistency checks |
<!-- end generated -->

## Workflows

`lcm workflow run NAME` runs a named graph of these commands and keeps every step's output with a summary table:

| Workflow | What it is for |
|---|---|
| `morning_check` | what broke overnight: data, tracking, deliverability trend, authentication, list health, live flows, running tests |
| `new_flow` | from one sentence to a checked plan, including how it collides with the flows already live |
| `quarterly_review` | retention, the next flows worth building, discount dependency, the experiment registry and the program's ROI |

Add your own as `workflows/*.yaml` in the workspace; the format is in `engine/workflows/runner.py`.
