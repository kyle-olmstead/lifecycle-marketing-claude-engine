# Quickstart

Ten minutes from a clone to a checked plan on a sample brand, then your own. You need Python 3.12 or later and
[uv](https://docs.astral.sh/uv/) (or plain `pip`; both paths are below). Nothing here sends a message or touches a
platform.

## 1. Install

```text
git clone https://github.com/kyle-olmstead/lifecycle-marketing-claude-engine.git
cd lifecycle-marketing-claude-engine
uv sync                      # creates .venv with the exact versions in uv.lock
uv run lcm doctor            # Python, packages, the email compiler, the workspace, which keys are set
```

Without uv: `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`, then `lcm doctor`. The commands
below are written as `lcm ...`; with uv, put `uv run` in front or activate `.venv` first.

`lcm doctor` ends with **Ready.** or lists what to fix. Outside a workspace it says it is using the bundled demo, which
is what you want for step 2.

## 2. Plan on a sample brand

Two fictional companies ship with their data, so you can judge the output before connecting anything:

| Workspace | Company | Stack |
|---|---|---|
| `examples/brands/northfield_dtc` | a coffee roaster | Shopify and Klaviyo, raw vendor exports in `exports/`, mapped to `data/` |
| `examples/brands/ridgeview_saas` | a B2B reporting tool | Customer.io and Postgres, product events in `data/` |

```text
cd examples/brands/northfield_dtc
lcm plan --text "a 3-touch abandoned cart flow with email and SMS" --data data
```

The plan lands in `runs/northfield_coffee_cart_abandon_1h/plan.md`: who enters and when they leave, every message
rendered in Klaviyo's template language with the cart shown from Shopify's own event, the holdout and when it can be
read, the compliance review, and a "Confirm before you build" list. Then try:

```text
lcm "make it 4 touches"                    # a revision of the plan you just made; approved copy stays approved
lcm simulate --program                     # the live flows in briefs/active/ together against one frequency policy
lcm size --baseline 0.31 --mde-rel 0.15 --holdout 0.10 --entries-per-month 350
lcm workflow run morning_check             # the daily check, every step's output kept with a summary
```

The finished runs, planned from written copy, are in each workspace's `runs/` folder.

## 3. Your own brand

```text
lcm init ~/acme --name Acme --platform klaviyo
cd ~/acme
```

`lcm init` writes `lmm.yaml` (your program: platform, warehouse dialect, sending gate, one frequency policy, offer
policy, measurement settings) and `brand.yaml` (logo, colors, fonts, footer, voice). Open both and fill in the blanks:
at least `brand.postal_address`, `sender_name` and your colors. Every plan in this folder starts from them.

```text
lcm import shopify ~/Downloads/shopify-export --out data     # or: klaviyo, stripe, hubspot, customerio, ga4, segment,
                                                             # rudderstack, or auto to detect each folder
lcm check-data data                                          # missing, late, malformed or future-dated rows, with fixes
lcm deliverability check acme.com --write-gate gate.json     # then set  sending_gate: gate.json  in lmm.yaml
lcm doctor
lcm plan --text "a 5-touch win-back for customers who have not ordered in 60 days" --data data
```

Without a gate on record (or with one older than 30 days) plans run as AMBER and mail engaged people only; typing
GREEN by hand also runs as AMBER, because only an observed check can open the full audience.

## 4. Where to go next

- What the engine plans, flow by flow: [`docs/FLOW_LIBRARY.md`](docs/FLOW_LIBRARY.md)
- Every command and what it does: [`docs/COMMANDS.md`](docs/COMMANDS.md), or `lcm --help`
- Reading a plan, revising it, and who writes the copy: [`docs/quickstart_for_marketers.md`](docs/quickstart_for_marketers.md)
- Vendor exports and what each one leaves out: [`docs/IMPORTING_DATA.md`](docs/IMPORTING_DATA.md)
- Using it inside Claude as a plugin instead of a terminal: the README, section 5
