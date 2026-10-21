"""The workspace: lmm.yaml supplies what every brief in the folder would otherwise have to repeat or guess."""
import json

import yaml

from engine import workspace
from engine.brief import load_brief, parse_text


def _ws(tmp_path, **changes):
    workspace.init(tmp_path, "Northfield", platform="klaviyo", business_model="ecommerce")
    cfg = yaml.safe_load((tmp_path / "lmm.yaml").read_text())
    for k, v in changes.items():
        cfg[k] = v
    (tmp_path / "lmm.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return workspace.Workspace(tmp_path)


def test_init_writes_a_valid_workspace(tmp_path):
    created = workspace.init(tmp_path, "Northfield", platform="braze", business_model="ecommerce")
    names = {p.relative_to(tmp_path).as_posix() for p in created}
    assert {"lmm.yaml", "brand.yaml", "data/README.md", "briefs/active/README.md", "runs/README.md"} <= names
    ws = workspace.Workspace(tmp_path)
    assert workspace.validate(ws) == []
    cfg = ws.config()
    assert cfg["brand"]["name"] == "Northfield" and cfg["platform"] == "braze"
    assert yaml.safe_load((tmp_path / "brand.yaml").read_text())["brand_name"] == "Northfield"
    try:
        workspace.init(tmp_path, "Again")
        raise AssertionError("init overwrote an existing workspace")
    except FileExistsError:
        pass


def test_validate_names_the_bad_field(tmp_path):
    ws = _ws(tmp_path, platform="mailchimp")
    errs = workspace.validate(ws)
    assert errs and any("platform" in e for e in errs)


def test_workspace_values_replace_assumptions_but_never_the_sentence(tmp_path):
    ws = _ws(tmp_path, sending_gate="AMBER")
    d = workspace.brief_defaults(ws)
    b = parse_text("Build a 3-touch abandoned cart flow", defaults=d)
    assumed = {a.field for a in b.assumptions}
    assert b.platform == "klaviyo" and "platform" not in assumed
    assert b.product_name == "Northfield" and "product_name" not in assumed
    assert b.sending_gate == "AMBER" and "sending_gate" not in assumed
    assert b.freq.collision_rule == "higher_priority_preempts" and (b.freq.window("sms").allowed_from, b.freq.window("sms").allowed_until) == ("09:00", "20:00")
    # a platform the sentence names beats the workspace's
    assert parse_text("Build a 3-touch abandoned cart flow in Braze", defaults=d).platform == "braze"
    # an explicit override beats both
    assert parse_text("Build a 3-touch abandoned cart flow", product_name="Other", defaults=d).product_name == "Other"


def test_a_gate_record_file_is_read_and_a_missing_one_is_unknown(tmp_path):
    rec = {"verdict": "GREEN", "assessed_at": "2026-10-20T12:00:00Z", "auth_label": "LIVE PULL", "domain": "northfield.example",
           "source": "lcm deliverability check northfield.example"}
    (tmp_path / "gate.json").write_text(json.dumps(rec))
    ws = _ws(tmp_path, sending_gate="gate.json")
    assert workspace.brief_defaults(ws)["sending_gate"]["verdict"] == "GREEN"
    b = parse_text("Build a 3-touch abandoned cart flow", defaults=workspace.brief_defaults(ws))
    assert b.sending_gate.verdict == "GREEN" and "sending_gate" not in {a.field for a in b.assumptions}
    (tmp_path / "gate.json").unlink()
    assert workspace.brief_defaults(ws)["sending_gate"] == "UNKNOWN"


def test_yaml_briefs_inherit_too(tmp_path):
    ws = _ws(tmp_path)
    brief = tmp_path / "briefs" / "active" / "winback.yaml"
    brief.write_text("product_name: Northfield\nflow_type: winback\ncore_conversion_event: order_completed\nchurn_threshold_days: 60\n")
    b = load_brief(brief, defaults=workspace.brief_defaults(ws))
    assert b.platform == "klaviyo" and "platform" not in {a.field for a in b.assumptions}
    assert b.business_model == "ecommerce"


def test_no_workspace_means_no_defaults(tmp_path):
    ws = workspace.find(start=tmp_path, env={})
    assert ws.fallback and workspace.brief_defaults(ws) == {}
    named = workspace.find(env={"LCM_WORKSPACE": str(tmp_path)})
    assert not named.fallback and named.root == tmp_path.resolve()
