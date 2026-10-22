"""Brand kits: schema validation with clear errors, WCAG 2.1 AA contrast, defaults, derived colors and the workspace loader."""
import json

import jsonschema
import pytest
import yaml

from tests.conftest import ROOT
from engine.render import brand as brand_mod
from engine.render.brand import (DEFAULT_BRAND, BrandKitError, contrast_ratio, find_brand, load_brand, parse_brand,
                                 relative_luminance)
from engine.workspace import Workspace

RIDGEVIEW = ROOT / "examples/brands/ridgeview_saas/brand.yaml"
NORTHFIELD = ROOT / "examples/brands/northfield_dtc/brand.yaml"
TEMPLATE = ROOT / "templates/brand/brand.yaml"
EM_DASH = chr(0x2014)


def test_schema_is_valid_2020_12():
    schema = json.loads((ROOT / "schemas/brand_kit.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["required"] == ["brand_name"]


def test_wcag_relative_luminance_and_contrast_match_the_published_values():
    assert relative_luminance("#FFFFFF") == pytest.approx(1.0) and relative_luminance("#000000") == 0
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)
    # the classic boundary: #767676 is the lightest grey that passes 4.5:1 on white, #777777 just fails
    assert contrast_ratio("#767676", "#FFFFFF") >= 4.5 > contrast_ratio("#777777", "#FFFFFF")
    assert contrast_ratio("#fff", "#000") == contrast_ratio("#000000", "#FFFFFF")


def test_neutral_default_reproduces_the_original_look():
    k = DEFAULT_BRAND
    assert k.is_default and k.logo is None and k.brand_name == "" and k.name_or("Acme") == "Acme"
    assert (k.light.primary, k.light.button_text, k.light.text, k.light.card) == ("#1F5F4A", "#FFFFFF", "#1C1C1C", "#FFFFFF")
    assert (k.dark.card, k.dark.text) == ("#1E1E1E", "#F2F2F2")
    assert k.fonts.family.startswith("Arial") and k.lang == "en"
    assert all(c.ok for c in k.contrast_report() if c.enforced)
    assert load_brand(None) is DEFAULT_BRAND


def test_template_kit_is_loadable_and_neutral():
    k = load_brand(TEMPLATE)
    assert k.brand_name == "Your Brand"
    assert (k.light, k.dark, k.fonts) == (DEFAULT_BRAND.light, DEFAULT_BRAND.dark, DEFAULT_BRAND.fonts)
    text = TEMPLATE.read_text()
    # every top-level field of the schema is explained in the template
    for key in json.loads((ROOT / "schemas/brand_kit.schema.json").read_text())["properties"]:
        assert f"{key}:" in text, key


@pytest.mark.parametrize("path", [RIDGEVIEW, NORTHFIELD, TEMPLATE])
def test_kit_files_are_valid_and_free_of_em_dashes(path):
    assert EM_DASH not in path.read_text(encoding="utf-8")
    assert not brand_mod.schema_errors(yaml.safe_load(path.read_text()))
    k = load_brand(path)
    assert all(c.ok for c in k.contrast_report() if c.enforced)


def test_example_kits_carry_every_section():
    r = load_brand(RIDGEVIEW)
    assert r.brand_name == "Ridgeview" and r.logo.alt == "Ridgeview" and r.logo.width == 140
    assert r.light.primary == "#2B4C7E" and r.dark.button_text == "#10203A"
    assert r.sender("marketing").from_address == "news@example.com" and r.sender("person").from_name == "Dana Ortiz"
    assert r.footer.postal_address.startswith("100 Example Way") and r.voice.sign_off == "The Ridgeview team"
    assert [s.label for s in r.social] == ["LinkedIn"]
    n = load_brand(NORTHFIELD)
    assert n.brand_name == "Northfield Coffee Roasters" and n.fonts.web_font_name == "Lora"
    assert n.fonts.family == "Lora, Georgia, 'Times New Roman', serif" and n.lang == "en-US"


@pytest.mark.parametrize("data, expected", [
    ({"brand_name": "X", "colors": {"primary": "green"}}, "colors.primary: 'green' is not a hex color"),
    ({"brand_name": "X", "colour": {}}, "Additional properties are not allowed ('colour' was unexpected)"),
    ({"brand_name": "Hi {{ first_name }}"}, "brand_name: words only"),
    ({"brand_name": "X", "footer": {"extra_lines": ["<b>bold</b>"]}}, "footer.extra_lines.0: words only"),
    ({"brand_name": "X", "logo": {"url": "http://x.example/l.png", "alt": "X", "width": 100}}, "logo.url"),
    ({"brand_name": "X", "logo": {"url": "https://x.example/l.png", "width": 100}}, "'alt' is a required property"),
    ({"brand_name": "X", "senders": {"marketing": {"from_name": "X", "from_address": "not-an-email"}}}, "is not an email address"),
    ({"brand_name": "X", "fonts": {"stack": "Arial; color: red"}}, "fonts.stack"),
    ({}, "'brand_name' is a required property"),
])
def test_schema_errors_name_the_field(data, expected):
    with pytest.raises(BrandKitError) as e:
        parse_brand(data, "kit.yaml")
    assert any(expected in msg for msg in e.value.errors), e.value.errors
    assert str(e.value).startswith("kit.yaml: the brand kit is not usable")


def test_every_problem_is_reported_at_once():
    with pytest.raises(BrandKitError) as e:
        parse_brand({"brand_name": "", "colors": {"text": "black", "card": "#12345"}}, "kit.yaml")
    assert len(e.value.errors) == 3


def test_button_text_below_aa_fails_with_ratio_and_citation():
    with pytest.raises(BrandKitError) as e:
        parse_brand({"brand_name": "X", "colors": {"primary": "#8FD3B6", "button_text": "#FFFFFF"}})
    msg = " ".join(e.value.errors)
    assert "light mode: button text on the button is #FFFFFF on #8FD3B6" in msg
    assert "4.5:1" in msg and "https://www.w3.org/TR/WCAG21/#contrast-minimum" in msg and "1.4.3" in msg


def test_dark_mode_text_below_aa_fails():
    with pytest.raises(BrandKitError) as e:
        parse_brand({"brand_name": "X", "colors": {"dark": {"text": "#555555", "card": "#333333"}}})
    assert any(m.startswith("dark mode: body text on the card") and "colors.dark" in m for m in e.value.errors)


def test_footer_text_on_the_background_is_checked():
    with pytest.raises(BrandKitError) as e:
        parse_brand({"brand_name": "X", "colors": {"background": "#333333"}})
    assert any("footer text on the background" in m for m in e.value.errors)


def test_non_text_button_contrast_is_reported_but_not_enforced():
    rows = {(r.mode, r.pair): r for r in DEFAULT_BRAND.contrast_report()}
    fill = rows[("dark", "button fill against the card (non-text, SC 1.4.11)")]
    assert not fill.ok and not fill.enforced and "advisory" in fill.describe()


def test_missing_colors_derive_from_the_given_ones():
    navy = parse_brand({"brand_name": "X", "colors": {"primary": "#2b4c7e"}})
    assert navy.light.primary == "#2B4C7E" and navy.light.link == "#2B4C7E"      # readable, so links use it
    assert navy.dark.primary == "#2B4C7E" and navy.dark.link == "#F2F2F2"         # navy is unreadable on the dark card
    mint = parse_brand({"brand_name": "X", "colors": {"primary": "#8FD3B6", "button_text": "#10203A"}})
    assert (mint.light.link, mint.dark.link) == ("#1C1C1C", "#F2F2F2")                # neither reads as a link: text colors
    assert parse_brand({"brand_name": "X", "colors": {"card": "#fff"}}).light.card == "#FFFFFF"


def test_yaml_and_file_errors_are_clear(tmp_path):
    bad = tmp_path / "brand.yaml"
    bad.write_text("brand_name: X\ncolors:\n  primary: [unclosed\n")
    with pytest.raises(BrandKitError) as e:
        load_brand(bad)
    assert "not valid YAML (line" in e.value.errors[0]
    with pytest.raises(BrandKitError, match="no such file"):
        load_brand(tmp_path / "missing.yaml")
    listy = tmp_path / "list.yaml"
    listy.write_text("- a\n- b\n")
    with pytest.raises(BrandKitError, match="key: value"):
        load_brand(listy)


def test_find_brand_reads_the_workspace(tmp_path):
    assert find_brand(Workspace(tmp_path)) is DEFAULT_BRAND                       # no kit: the neutral default
    (tmp_path / "brand.yaml").write_text("brand_name: Acme\n")
    assert find_brand(Workspace(tmp_path)).brand_name == "Acme"
    (tmp_path / "kits").mkdir()
    (tmp_path / "kits" / "acme.yaml").write_text("brand_name: Acme Two\n")
    (tmp_path / "lmm.yaml").write_text("brand_kit: kits/acme.yaml\n")
    assert find_brand(Workspace(tmp_path)).brand_name == "Acme Two"              # lmm.yaml names the kit
    (tmp_path / "lmm.yaml").write_text("brand_kit: kits/nope.yaml\n")
    with pytest.raises(BrandKitError, match="does not exist"):
        find_brand(Workspace(tmp_path))


def test_the_demo_workspace_has_the_ridgeview_kit(tmp_path, monkeypatch):
    from engine.paths import DEMO
    k = find_brand(Workspace(DEMO, demo=True))
    assert k.brand_name == "Ridgeview" and k.source.endswith("brand.yaml")
    (tmp_path / "brand.yaml").write_text("brand_name: Named Workspace\n")
    monkeypatch.setenv("LCM_WORKSPACE", str(tmp_path))
    assert find_brand().brand_name == "Named Workspace"        # no argument: engine.workspace.find()


def test_voice_brief_and_voice_check():
    k = load_brand(RIDGEVIEW)
    brief = k.voice_brief()
    assert "no exclamation marks" in brief and "'stakeholders' -> 'whoever asks you for the numbers'" in brief
    found = k.check_voice(["Unlock the Monday numbers for your stakeholders", "Nothing else"])
    assert "voice: 'unlock' is a forbidden phrase for Ridgeview" in found
    assert "voice: write 'whoever asks you for the numbers' instead of 'stakeholders'" in found
    assert k.check_voice(["unlocked doors"]) == []     # whole words only
    assert DEFAULT_BRAND.voice_brief() == "" and DEFAULT_BRAND.check_voice(["unlock"]) == []


def test_cli_prints_the_contrast_table(tmp_path, capsys):
    assert brand_mod._cli([str(RIDGEVIEW)]) == 0
    out = capsys.readouterr().out
    assert "button text on the button: #FFFFFF on #2B4C7E" in out and "OK:" in out
    bad = tmp_path / "brand.yaml"
    bad.write_text("brand_name: X\ncolors:\n  text: '#EEEEEE'\n")
    assert brand_mod._cli([str(bad)]) == 1
    assert "body text on the card" in capsys.readouterr().out
    assert brand_mod._cli([str(RIDGEVIEW), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kit"]["brand_name"] == "Ridgeview"
