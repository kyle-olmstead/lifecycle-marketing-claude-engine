"""Dry-run a flow before anyone builds it: structure, journeys and payload syntax.

Three passes, all offline:

1. Structure (any flow spec, including hand-written ones): valid against
   schemas/flow_spec.schema.json; unique step ids; every branch target exists or is an exit; no
   unreachable step; the holdout split comes before the first message; the nominal schedule never
   breaks the weekly frequency cap.
2. Journeys (when the spec has a simulation sidecar, which the teammate writes): synthetic people
   walk the flow minute by minute against machine-readable rules for every condition and branch.
   Holdout members receive nothing; nobody is messaged on a channel without consent; a person who
   does the core action receives nothing after it; nobody exceeds the frequency cap, the touch count
   or the send hour; the sunset applies only to people who did not click.
3. Payloads (with --payloads): every ESP request body in a folder validates against
   config/esp_payload_schemas.json.

    python3 scripts/campaign_simulator.py out/ridgeview_winback_14d/flow_spec.json
    python3 scripts/campaign_simulator.py examples/flows/winback_klaviyo.json --json
    python3 scripts/campaign_simulator.py out/x/flow_spec.json --rules out/x/simulation.json --payloads out/x/trigger_payloads

Exit code 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402

FLOW_SCHEMA = json.loads((ROOT / "schemas" / "flow_spec.schema.json").read_text(encoding="utf-8"))
PAYLOAD_SCHEMA_PATH = ROOT / "config" / "esp_payload_schemas.json"
DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def hours(iso: Optional[str]) -> float:
    m = DURATION.match(iso or "")
    if not m:
        raise ValueError(f"not an ISO 8601 duration: {iso!r}")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60


def _check(name: str, ok: bool, detail: str, warn: bool = False) -> Dict[str, str]:
    return {"name": name, "status": "pass" if ok else ("warn" if warn else "fail"), "detail": detail}


def _is_exit(target: str) -> bool:
    return target.strip().lower().startswith("exit")


CAPPED = ("email", "sms", "push")  # in-app messages are capped per session, not per week


def _cap(spec: Dict[str, Any], rules: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Tuple[str, ...]]:
    """(weekly cap, channels it covers). A cap written about email covers email only."""
    text = spec.get("frequency", {}).get("cap", "").lower()
    channels = ("email",) if "email" in text and "message" not in text else CAPPED
    if rules and rules.get("frequency_cap_per_week"):
        return int(rules["frequency_cap_per_week"]), channels
    m = re.search(r"(\d+)\s+(?:[\w-]+\s+){0,4}(?:per|a)\s+week", text)
    return (int(m.group(1)) if m else None), channels


def _paths(steps: List[Dict[str, Any]], worst_case: bool) -> List[List[Tuple[float, str]]]:
    """Every route through the flow as (hour, channel) sends. worst_case: awaited events happen at once."""
    index = {s["id"]: i for i, s in enumerate(steps)}
    out: List[List[Tuple[float, str]]] = []

    def go(i: int, t: float, acc: List[Tuple[float, str]], depth: int) -> None:
        if i >= len(steps) or depth > 60:
            out.append(acc)
            return
        s = steps[i]
        w = s.get("wait", {})
        if w.get("type") == "fixed":
            t += hours(w.get("duration"))
        elif w.get("type") == "until_event" and not worst_case:
            t += hours(w.get("timeout"))
        elif w.get("type") == "until_time_local":
            t += 0 if worst_case else 12
        if s.get("channel") not in ("none", None) and "fallback" not in (s.get("condition") or ""):
            acc = acc + [(t, s["channel"])]
        br = s.get("branch")
        if br:
            nxt = []
            for tgt in (br.get("yes", ""), br.get("no", "")):
                if _is_exit(tgt):
                    out.append(acc)
                    continue
                first = tgt.split(" ")[0].strip()
                nxt.append(index.get(first, i + 1))
            for j in dict.fromkeys(nxt):
                go(j, t, acc, depth + 1)
            return
        go(i + 1, t, acc, depth + 1)

    if steps:
        go(0, 0.0, [], 0)
    return out


# ----------------------------------------------------------------------------------------------
# structure
# ----------------------------------------------------------------------------------------------

def structural(spec: Dict[str, Any], rules: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    checks: List[Dict[str, str]] = []
    errs = sorted(jsonschema.Draft202012Validator(FLOW_SCHEMA).iter_errors(spec), key=lambda e: list(e.path))
    checks.append(_check("flow spec matches schemas/flow_spec.schema.json", not errs,
                         "valid" if not errs else "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errs[:5])))
    if errs and not isinstance(spec.get("steps"), list):
        return checks
    steps = spec.get("steps", [])
    ids = [s.get("id") for s in steps]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    checks.append(_check("step ids are unique", not dup, "unique" if not dup else f"duplicated: {dup}"))
    index = {sid: i for i, sid in enumerate(ids)}
    bad_targets = []
    edges: Dict[int, List[int]] = {i: [] for i in range(len(steps))}
    for i, s in enumerate(steps):
        br = s.get("branch")
        if br:
            for tgt in (br.get("yes", ""), br.get("no", "")):
                first = tgt.split(" ")[0].strip()
                if _is_exit(tgt):
                    continue
                if first in index:
                    edges[i].append(index[first])
                else:
                    bad_targets.append(f"{s['id']} -> {tgt!r}")
        elif i + 1 < len(steps):
            edges[i].append(i + 1)
    id_like = [b for b in bad_targets if re.fullmatch(r"[a-z0-9_]+", b.split(" -> ")[1].strip("'"))]
    prose = [b for b in bad_targets if b not in id_like]
    checks.append(_check("every branch target is a step or an exit", not id_like,
                         "all resolve" if not bad_targets else ("; ".join(id_like) if id_like else "every step-id target resolves")))
    if prose:
        checks.append(_check("branch targets are machine-readable", False,
                             "free-text target(s), read as 'continue': " + "; ".join(prose) + ". Name a step id or 'exit (...)'", warn=True))
    seen, stack = set(), [0] if steps else []
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack += edges[n]
        if steps[n].get("branch") is None and n + 1 < len(steps):
            stack.append(n + 1)
    unreachable = [ids[i] for i in range(len(steps)) if i not in seen]
    checks.append(_check("every step is reachable", not unreachable, "all reachable" if not unreachable else f"unreachable: {unreachable}"))
    sending = [i for i, s in enumerate(steps) if s.get("channel") not in ("none", None)]
    hold = spec.get("holdout", {})
    if hold.get("percent", 0) > 0:
        split = [i for i, s in enumerate(steps) if s.get("branch") and "holdout" in s["branch"].get("condition", "").lower()]
        at_entry = [f["condition"] for f in spec.get("entry_filters", []) if "holdout" in f.get("condition", "").lower()]
        ok = bool(at_entry) or (bool(split) and (not sending or split[0] < sending[0]))
        detail = (f"excluded at entry ({at_entry[0]}), assigned before entry" if at_entry else
                  f"{ids[split[0]]} at position {split[0] + 1}" if ok else "no holdout branch or entry filter before the first sending step")
        checks.append(_check("holdout is held out before the first message", ok, detail))
    ex = spec.get("exit", {})
    checks.append(_check("exit on goal is immediate", bool(ex.get("unenroll_immediately")),
                         f"goal: {ex.get('goal')}" if ex.get("unenroll_immediately") else "unenroll_immediately is false: people keep receiving messages after they convert",
                         warn=True))
    for i in sending:
        s = steps[i]
        if s["channel"] in ("sms", "push"):
            fallback = any(steps[j].get("channel") == "email" and steps[j].get("wait", {}).get("type") == "none" for j in range(i + 1, min(i + 2, len(steps))))
            checks.append(_check(f"{s['id']}: people without {s['channel']} consent are covered", fallback,
                                 "email fallback follows" if fallback else "no email fallback: people without consent skip this touch", warn=True))
    cap, capped = _cap(spec, rules)

    def densest(paths: List[List[Tuple[float, str]]]) -> int:
        best = 0
        for p in paths:
            ts = [t for t, ch in p if ch in capped]
            best = max([best] + [sum(1 for x in ts if a <= x < a + 168) for a in ts])
        return best

    nominal = _paths(steps, worst_case=False)
    if cap:
        n = densest(nominal)
        checks.append(_check("nominal schedule respects the weekly frequency cap", n <= cap,
                             f"at most {n} {'/'.join(capped)} message(s) in any 7 days on any route, cap {cap}"))
        w = densest(_paths(steps, worst_case=True))
        if w > cap:
            checks.append(_check("frequency cap if awaited events happen at once", False,
                                 f"up to {w} in 7 days on the fastest route; the platform's frequency limit must be on to hold the cap", warn=True))
    longest = max(nominal, key=len, default=[])
    if longest:
        checks.append(_check("journey length", True, f"{len(longest)} touch(es) over {max(t for t, _ in longest) / 24:.0f} days on the longest route", warn=True))
    return checks


# ----------------------------------------------------------------------------------------------
# journeys
# ----------------------------------------------------------------------------------------------

def evaluate(rule: Optional[Dict[str, Any]], p: Dict[str, Any], t: float) -> bool:
    if rule is None:
        return True
    if "not" in rule:
        return not evaluate(rule["not"], p, t)
    if "all" in rule:
        return all(evaluate(r, p, t) for r in rule["all"])
    if "any" in rule:
        return any(evaluate(r, p, t) for r in rule["any"])
    if "holdout" in rule:
        return bool(p.get("holdout")) == bool(rule["holdout"])
    if "consent" in rule:
        return rule["consent"] in p.get("consents", [])
    if "event_since_entry" in rule:
        return any(e["name"] == rule["event_since_entry"] and 0 < e["at"] <= t for e in p.get("events", []))
    if "attribute_in" in rule:
        a = rule["attribute_in"]
        return p.get("attributes", {}).get(a["name"]) in a["values"]
    raise ValueError(f"unknown rule {rule}")


def _next_local(t: float, hhmm: str, entry_hour: float) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    target = h + m / 60
    clock = (entry_hour + t) % 24
    delta = (target - clock) % 24
    return t + delta


def walk(spec: Dict[str, Any], rules: Dict[str, Any], p: Dict[str, Any], entry_hour: float = 5.0) -> Dict[str, Any]:
    steps = spec["steps"]
    ids = {s["id"]: i for i, s in enumerate(steps)}
    srules = rules.get("steps", {})
    goal = rules.get("goal_event")
    display = rules.get("in_app_display_days", 3) * 24
    email_filter = any("email" in f.get("condition", "").lower() for f in spec.get("ongoing_filters", []))
    log: List[Dict[str, Any]] = []
    t = 0.0
    i = 0
    outcome = "completed"
    guard = 0
    while i < len(steps):
        guard += 1
        if guard > 200:
            outcome = "loop detected"
            break
        s = steps[i]
        w = s.get("wait", {"type": "none"})
        if w["type"] == "fixed":
            t += hours(w["duration"])
        elif w["type"] == "until_time_local":
            t = _next_local(t, w["local_time"], entry_hour)
        elif w["type"] == "until_event":
            hit = [e["at"] for e in p.get("events", []) if e["name"] == w["event"] and t < e["at"] <= t + hours(w["timeout"])]
            t = min(hit) if hit else t + hours(w["timeout"])
        if goal and evaluate({"event_since_entry": goal}, p, t):
            outcome = "converted"
            break
        r = srules.get(s["id"], {})
        if s.get("branch"):
            yes = evaluate(r.get("branch"), p, t) if "branch" in r else False
            target = s["branch"]["yes" if yes else "no"]
            log.append({"step": s["id"], "at_h": round(t, 2), "action": f"branch {'yes' if yes else 'no'}"})
            if _is_exit(target):
                outcome = target.strip()
                break
            i = ids[target.split(" ")[0]]
            continue
        if not evaluate(r.get("condition"), p, t):
            log.append({"step": s["id"], "at_h": round(t, 2), "action": "skipped (condition false)"})
            i += 1
            continue
        ch = s.get("channel")
        if ch == "none":
            log.append({"step": s["id"], "at_h": round(t, 2), "action": "update"})
        elif ch == "email" and email_filter and "email" not in p.get("consents", []):
            log.append({"step": s["id"], "at_h": round(t, 2), "action": "suppressed: every-step email filter"})
        elif ch in ("email", "sms", "push"):
            # the step sends whenever its own conditions pass: consent is the flow's job, and a missing check shows as a leak
            log.append({"step": s["id"], "at_h": round(t, 2), "action": f"sent {ch}"})
        elif ch == "in_app":
            seen = [e["at"] for e in p.get("events", []) if e["name"] == "session_started" and t < e["at"] <= t + display]
            log.append({"step": s["id"], "at_h": round(min(seen) if seen else t, 2), "action": "displayed in_app" if seen else "in_app expired unseen"})
        i += 1
    return {"outcome": outcome, "log": log, "sends": [x for x in log if x["action"].startswith(("sent", "displayed"))]}


def personas(spec: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    # what people do (core_event) is kept apart from what the flow exits on (goal_event), so a wrong exit shows up
    goal = rules.get("core_event") or rules.get("goal_event", "core_action")
    full = ["email", "sms", "push"]
    probe = walk(spec, rules, {"consents": full, "events": []})
    sends = probe["sends"]
    first = sends[0]["at_h"] if sends else 24.0
    last_email = max([x["at_h"] for x in sends if x["action"] == "sent email"] or [first])
    sessions = [{"name": "session_started", "at": 30.0 + 48 * k} for k in range(25)]
    return {
        "holdout member": {"holdout": True, "consents": full, "events": []},
        "email only, never returns": {"consents": ["email"], "events": []},
        "every channel, never returns": {"consents": full, "events": []},
        "returns after the first touch": {"consents": full, "events": [{"name": goal, "at": first + 20}]},
        "no consent on any channel": {"consents": [], "events": []},
        "clicks the last email": {"consents": ["email"], "events": [{"name": "message_clicked", "at": last_email + 2}]},
        "opens the app but skips the core action": {"consents": ["email"], "events": sessions},
    }


def behavioral(spec: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    checks: List[Dict[str, str]] = []
    journeys = {name: walk(spec, rules, p) for name, p in personas(spec, rules).items()}
    cap = rules.get("frequency_cap_per_week")
    hour = rules.get("send_hour_local")
    quiet_start, quiet_end = rules.get("quiet_hours_local", [21, 8])
    ps = personas(spec, rules)
    h = journeys["holdout member"]
    checks.append(_check("holdout members receive nothing", not h["sends"] and "holdout" in h["outcome"],
                         f"outcome: {h['outcome']}, {len(h['sends'])} message(s)"))
    nc = journeys["no consent on any channel"]
    checks.append(_check("no message without consent", not [x for x in nc["sends"] if x["action"].startswith("sent")],
                         f"{len(nc['sends'])} message(s) to a person with no consent"))
    leaks = []
    for name, j in journeys.items():
        consents = ps[name].get("consents", [])
        for x in j["sends"]:
            ch = x["action"].split(" ")[-1]
            if ch in ("email", "sms", "push") and ch not in consents:
                leaks.append(f"{name}: {x['step']}")
    checks.append(_check("every send matches a consent", not leaks, "all matched" if not leaks else "; ".join(leaks)))
    r = journeys["returns after the first touch"]
    goal_at = ps["returns after the first touch"]["events"][0]["at"]
    after = [x for x in r["sends"] if x["at_h"] > goal_at]
    checks.append(_check("nothing is sent after the core action", r["outcome"] == "converted" and not after,
                         f"outcome: {r['outcome']}; {len(after)} message(s) after converting"))
    if cap:
        worst = 0
        for j in journeys.values():
            ts = [x["at_h"] for x in j["sends"] if x["action"].startswith("sent")]
            for a in ts:
                worst = max(worst, sum(1 for b in ts if a <= b < a + 168))
        checks.append(_check("frequency cap holds for every persona", worst <= cap, f"most in any 7 days: {worst}, cap {cap}"))
    if hour is not None:
        off = [f"{n}: {x['step']}" for n, j in journeys.items() for x in j["sends"]
               if x["action"].startswith("sent") and round((5.0 + x["at_h"]) % 24, 2) != float(hour)]
        checks.append(_check(f"messages send at {hour:02d}:00 local", not off, "all on time" if not off else "; ".join(off[:5])))
        quiet = [f"{n}: {x['step']}" for n, j in journeys.items() for x in j["sends"] if x["action"].startswith("sent")
                 and ((5.0 + x["at_h"]) % 24 >= quiet_start or (5.0 + x["at_h"]) % 24 < quiet_end)]
        checks.append(_check("nothing sends in quiet hours", not quiet, "none" if not quiet else "; ".join(quiet[:5])))
    mx = rules.get("max_touches")
    if mx:
        over = [n for n, j in journeys.items() if len(j["sends"]) > mx]
        checks.append(_check("no persona gets more than the touch count", not over, f"max {mx}" if not over else f"over: {over}"))
    if "sunset" in {s["id"] for s in spec["steps"]}:
        clicked = [x for x in journeys["clicks the last email"]["log"] if x["step"] == "sunset" and x["action"] == "update"]
        silent = [x for x in journeys["email only, never returns"]["log"] if x["step"] == "sunset" and x["action"] == "update"]
        checks.append(_check("sunset applies only to people who did not click", bool(silent) and not clicked,
                             f"non-clicker sunset: {bool(silent)}; clicker sunset: {bool(clicked)}"))
    loops = [n for n, j in journeys.items() if j["outcome"] == "loop detected"]
    checks.append(_check("no journey loops", not loops, "none" if not loops else str(loops)))
    summary = {n: {"outcome": j["outcome"], "messages": [f"{x['step']} day {x['at_h'] / 24:.1f}" for x in j["sends"]]} for n, j in journeys.items()}
    return checks, summary


# ----------------------------------------------------------------------------------------------
# payloads
# ----------------------------------------------------------------------------------------------

def payload_platform(req: Dict[str, Any]) -> Optional[str]:
    url = req.get("url", "")
    for host, name in (("klaviyo.com", "klaviyo_create_event"), ("hubapi.com/events", "hubspot_send_events"),
                       ("braze.com/users/track", "braze_users_track"), ("cdp.customer.io", "customerio_pipelines_track"),
                       ("cdp-eu.customer.io", "customerio_pipelines_track"), ("track.customer.io", "customerio_track_v1")):
        if host in url:
            if name == "hubspot_send_events" and url.rstrip("/").endswith("/batch"):
                return "hubspot_send_events_batch"
            return name
    return None


def validate_request(req: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """(schema name, error messages) for one request built by tools/event_payloads.py."""
    name = payload_platform(req)
    if name is None:
        return None, [f"unrecognized endpoint {req.get('url')}"]
    schema = json.loads(PAYLOAD_SCHEMA_PATH.read_text(encoding="utf-8"))
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{name}"}
    validator = jsonschema.Draft202012Validator(sub, format_checker=jsonschema.FormatChecker())
    return name, [f"{'/'.join(map(str, e.path)) or '(body)'}: {e.message}" for e in validator.iter_errors(req.get("body"))]


def check_payloads(folder: Path) -> List[Dict[str, str]]:
    checks = []
    files = sorted(Path(folder).glob("*.json"))
    for f in files:
        name, errs = validate_request(json.loads(f.read_text(encoding="utf-8")))
        checks.append(_check(f"payload {f.name}" + (f" ({name})" if name else ""), not errs, "valid" if not errs else errs[0]))
    if not files:
        checks.append(_check("payloads", False, f"no .json files in {folder}", warn=True))
    return checks


def simulate(spec: Dict[str, Any], rules: Optional[Dict[str, Any]] = None, payloads: Optional[Path] = None) -> Dict[str, Any]:
    checks = structural(spec, rules)
    journeys: Dict[str, Any] = {}
    if rules and not any(c["status"] == "fail" for c in checks[:3]):
        b, journeys = behavioral(spec, rules)
        checks += b
    elif not rules:
        checks.append(_check("journeys", True, "no simulation rules: structure checked only (the teammate writes simulation.json)", warn=True))
    if payloads:
        checks += check_payloads(payloads)
    status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"
    return {"flow": spec.get("flow"), "status": status, "checks": checks, "journeys": journeys}


def render(r: Dict[str, Any]) -> str:
    mark = {"pass": "PASS", "warn": "NOTE", "fail": "FAIL"}
    lines = [f"Simulation: {r['flow']}: {r['status'].upper()}", ""]
    lines += [f"  {mark[c['status']]:4}  {c['name']}: {c['detail']}" for c in r["checks"]]
    if r["journeys"]:
        lines += ["", "Journeys:"]
        for n, j in r["journeys"].items():
            lines.append(f"  {n}: {j['outcome']}; " + (", ".join(j["messages"]) if j["messages"] else "no messages"))
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Dry-run a flow spec: structure, synthetic journeys, payload syntax")
    ap.add_argument("flow_spec")
    ap.add_argument("--rules", help="simulation.json written by the teammate (default: next to the flow spec, if present)")
    ap.add_argument("--payloads", help="folder of ESP request bodies to validate")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    spec_path = Path(a.flow_spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    rules_path = Path(a.rules) if a.rules else spec_path.with_name("simulation.json")
    rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else None
    r = simulate(spec, rules, Path(a.payloads) if a.payloads else None)
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 1 if r["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
