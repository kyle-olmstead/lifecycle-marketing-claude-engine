"""Brand kit: one brand's logo, colors, fonts, footer, voice, senders and social links, loaded from brand.yaml.

Every email the engine renders takes its look from a brand kit (engine/render/mjml_render.py builds the MJML from
it). A kit is a YAML file checked against schemas/brand_kit.schema.json, so a typo or a color written as "green"
fails with the field's name instead of producing an odd email. Then the colors are checked for contrast: text, link
and button-label colors must reach 4.5:1 against what they sit on, in light mode and in dark mode, the WCAG 2.1 AA
minimum for normal text (Success Criterion 1.4.3, https://www.w3.org/TR/WCAG21/#contrast-minimum, with relative
luminance and contrast ratio as defined at https://www.w3.org/TR/WCAG21/#dfn-relative-luminance and
https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio). A kit that fails is rejected with every failing pair and its ratio.
The button's fill against the card is also measured against the 3:1 non-text minimum (SC 1.4.11) but only reported:
the button's label already identifies it, so WCAG does not require a visible boundary.

With no kit, the neutral default (DEFAULT_BRAND) reproduces the engine's original look: a white card, near-black
Arial text, a dark green button, no logo, and dark-mode colors for clients that support them.

Only brand_name is required in a kit; anything left out comes from the neutral default, and a few colors derive from
the ones given (a missing link color uses the button color when that is readable, otherwise the text color).

Usage:

    from engine.render.brand import load_brand, find_brand
    kit = load_brand("examples/brands/ridgeview_saas/brand.yaml")   # or load_brand(None) for the neutral default
    kit = find_brand()            # the current workspace's kit (brand_kit in lmm.yaml, else brand.yaml), or the default
    kit.light.primary             # '#2B4C7E'
    for row in kit.contrast_report():
        print(row.describe())

    python3 engine/render/brand.py examples/brands/ridgeview_saas/brand.yaml   # validate and print the contrast table
    python3 engine/render/brand.py                                            # the current workspace's kit
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import copy
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

import yaml

from engine.paths import SCHEMAS, TEMPLATES

SCHEMA_PATH = SCHEMAS / "brand_kit.schema.json"
TEMPLATE_PATH = TEMPLATES / "brand" / "brand.yaml"
BRAND_FILE = "brand.yaml"
WORKSPACE_KEY = "brand_kit"          # lmm.yaml: brand_kit: path/to/brand.yaml (relative to the workspace folder)

AA_NORMAL_TEXT = 4.5                 # WCAG 2.1 SC 1.4.3 Contrast (Minimum), level AA, normal-size text
NON_TEXT_MINIMUM = 3.0               # WCAG 2.1 SC 1.4.11 Non-text Contrast, level AA (reported, not enforced)
WCAG_CONTRAST_URL = "https://www.w3.org/TR/WCAG21/#contrast-minimum"
WCAG_NON_TEXT_URL = "https://www.w3.org/TR/WCAG21/#non-text-contrast"

COLOR_ROLES = ("primary", "button_text", "text", "background", "card", "link")

# The neutral default: the engine's original look (engine/agents/copywriter.py before brand kits), written as a kit.
DEFAULT_KIT: Dict[str, Any] = {
    "brand_name": "",   # empty: the renderer uses the brief's product name
    "lang": "en",
    "colors": {
        "primary": "#1F5F4A", "button_text": "#FFFFFF", "text": "#1C1C1C", "background": "#FFFFFF", "card": "#FFFFFF",
        "link": "#1F5F4A",
        "dark": {"primary": "#1F5F4A", "button_text": "#FFFFFF", "text": "#F2F2F2", "background": "#121212", "card": "#1E1E1E",
                 "link": "#8FD3B6"},
    },
    "fonts": {"stack": "Arial, Helvetica, sans-serif"},
}
DARK_FALLBACK = {"text": "#F2F2F2", "background": "#121212", "card": "#1E1E1E"}


class BrandKitError(ValueError):
    """A brand kit that cannot be used, with every problem found (not just the first)."""

    def __init__(self, source: str, errors: Iterable[str]) -> None:
        self.source = source
        self.errors = list(errors)
        super().__init__(f"{source}: the brand kit is not usable:\n  - " + "\n  - ".join(self.errors))


# ----------------------------------------------------------------------------------------------
# the kit, typed
# ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    primary: str
    button_text: str
    text: str
    background: str
    card: str
    link: str


@dataclass(frozen=True)
class Logo:
    url: str
    alt: str
    width: int
    height: Optional[int] = None


@dataclass(frozen=True)
class Fonts:
    stack: str
    web_font_name: Optional[str] = None
    web_font_url: Optional[str] = None

    @property
    def family(self) -> str:
        """The CSS font-family value: the web font first when there is one, then the web-safe stack."""
        return f"{self.web_font_name}, {self.stack}" if self.web_font_name else self.stack


@dataclass(frozen=True)
class Footer:
    legal_name: Optional[str] = None
    postal_address: Optional[str] = None
    extra_lines: Tuple[str, ...] = ()


@dataclass(frozen=True)
class VocabularySwap:
    avoid: str
    use_instead: str


@dataclass(frozen=True)
class Voice:
    register: Optional[str] = None
    vocabulary: Tuple[VocabularySwap, ...] = ()
    forbidden_phrases: Tuple[str, ...] = ()
    sign_off: Optional[str] = None


@dataclass(frozen=True)
class Sender:
    from_name: str
    from_address: str
    reply_to: Optional[str] = None


@dataclass(frozen=True)
class SocialLink:
    label: str
    url: str


@dataclass(frozen=True)
class ContrastCheck:
    """One measured color pair. enforced pairs fail the kit below the minimum; the others are reported only."""
    mode: str            # light | dark
    pair: str            # what is being read, in words
    foreground: str
    background: str
    ratio: float
    minimum: float
    enforced: bool

    @property
    def ok(self) -> bool:
        return self.ratio >= self.minimum

    def describe(self) -> str:
        verdict = "pass" if self.ok else ("FAIL" if self.enforced else "below (advisory)")
        return f"{self.mode:5} {self.pair}: {self.foreground} on {self.background} = {self.ratio:.2f}:1 (needs {self.minimum}:1) {verdict}"

    def failure(self) -> str:
        return (f"{self.mode} mode: {self.pair} is {self.foreground} on {self.background}, a contrast of {self.ratio:.2f}:1; "
                f"WCAG 2.1 AA needs at least {self.minimum}:1 for normal text (Success Criterion 1.4.3, {WCAG_CONTRAST_URL}). "
                f"Change one of the two colors{' under colors.dark' if self.mode == 'dark' else ''}.")


@dataclass(frozen=True)
class BrandKit:
    brand_name: str
    lang: str
    light: Palette
    dark: Palette
    fonts: Fonts
    website: Optional[str] = None
    logo: Optional[Logo] = None
    footer: Footer = field(default_factory=Footer)
    voice: Voice = field(default_factory=Voice)
    senders: Mapping[str, Sender] = field(default_factory=dict)
    social: Tuple[SocialLink, ...] = ()
    source: Optional[str] = None   # the file it came from; None for the neutral default

    @property
    def is_default(self) -> bool:
        return self.source is None

    def name_or(self, product_name: str) -> str:
        """The brand name, or the brief's product name for the neutral default (which has no name of its own)."""
        return self.brand_name or product_name

    def sender(self, role: str = "marketing") -> Optional[Sender]:
        return self.senders.get(role)

    def contrast_report(self) -> List[ContrastCheck]:
        return contrast_report(self.light, self.dark)

    def voice_brief(self) -> str:
        """The voice in a few lines, for a writer's copy brief. Empty when the kit says nothing about voice."""
        v = self.voice
        lines = []
        if v.register:
            lines.append(f"Register: {v.register}.")
        if v.vocabulary:
            lines.append("Never write, and write instead: " + "; ".join(f"'{s.avoid}' -> '{s.use_instead}'" for s in v.vocabulary) + ".")
        if v.forbidden_phrases:
            lines.append("Forbidden phrases: " + ", ".join(v.forbidden_phrases) + ".")
        if v.sign_off:
            lines.append(f"Sign-off, when a message signs off: {v.sign_off}.")
        return "\n".join(lines)

    def check_voice(self, texts: Iterable[str]) -> List[str]:
        """Forbidden phrases and avoided vocabulary found in the given copy, as warnings with the replacement to use."""
        body = "\n".join(t for t in texts if isinstance(t, str))
        found: List[str] = []
        for phrase in self.voice.forbidden_phrases:
            if _contains(body, phrase):
                found.append(f"voice: '{phrase}' is a forbidden phrase for {self.brand_name or 'this brand'}")
        for swap in self.voice.vocabulary:
            if _contains(body, swap.avoid):
                found.append(f"voice: write '{swap.use_instead}' instead of '{swap.avoid}'")
        return found

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _contains(text: str, phrase: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I) is not None


# ----------------------------------------------------------------------------------------------
# contrast (WCAG 2.1)
# ----------------------------------------------------------------------------------------------

def normalize_color(value: str) -> str:
    """'#abc' -> '#AABBCC'; '#1f5f4a' -> '#1F5F4A'."""
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", v):
        raise ValueError(f"{value!r} is not a hex color like #1F5F4A")
    return "#" + v.upper()


def relative_luminance(color: str) -> float:
    """WCAG 2.1 relative luminance of an sRGB color (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance).

    The WCAG 2.1 text gives 0.03928 as the linear-segment threshold; later errata use the sRGB standard's 0.04045.
    No 8-bit channel value falls between the two (10/255 = 0.0392, 11/255 = 0.0431), so the result is the same."""
    hexv = normalize_color(color)[1:]
    channels = []
    for i in (0, 2, 4):
        c = int(hexv[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    """(L1 + 0.05) / (L2 + 0.05) with L1 the lighter color (https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio); 1 to 21."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def contrast_report(light: Palette, dark: Palette) -> List[ContrastCheck]:
    rows: List[ContrastCheck] = []
    for mode, p in (("light", light), ("dark", dark)):
        pairs = [
            ("body text on the card", p.text, p.card, AA_NORMAL_TEXT, True),
            ("footer text on the background", p.text, p.background, AA_NORMAL_TEXT, True),
            ("button text on the button", p.button_text, p.primary, AA_NORMAL_TEXT, True),
            ("links on the card", p.link, p.card, AA_NORMAL_TEXT, True),
            ("links on the background (footer)", p.link, p.background, AA_NORMAL_TEXT, True),
            ("button fill against the card (non-text, SC 1.4.11)", p.primary, p.card, NON_TEXT_MINIMUM, False),
        ]
        for pair, fg, bg, minimum, enforced in pairs:
            rows.append(ContrastCheck(mode, pair, fg, bg, round(contrast_ratio(fg, bg), 2), minimum, enforced))
    return rows


def _readable(color: str, *backgrounds: str) -> bool:
    return all(contrast_ratio(color, bg) >= AA_NORMAL_TEXT for bg in backgrounds)


# ----------------------------------------------------------------------------------------------
# validation and loading
# ----------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _validator():
    import jsonschema
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _friendly(error: Any) -> str:
    path = ".".join(str(p) for p in error.absolute_path) or "(top level)"
    sub = error.schema if isinstance(error.schema, dict) else {}
    if error.validator == "pattern":
        pattern = sub.get("pattern", "")
        if pattern.startswith("^#("):
            return f"{path}: {error.instance!r} is not a hex color; write it as #RRGGBB, for example #1F5F4A"
        if pattern.startswith("^https://"):
            return f"{path}: {error.instance!r} must be a full https:// address with no spaces or quotes"
        if pattern.startswith("^(?!"):
            return f"{path}: words only, please: no HTML and no template code ({{{{ }}}}, {{% %}}, %% %%, ${{ }}); the renderer adds the platform's template code"
        if "@" in pattern:
            return f"{path}: {error.instance!r} is not an email address"
        if pattern.startswith("^[A-Za-z0-9 ,"):
            return f"{path}: {error.instance!r}: use font names separated by commas (letters, digits, spaces, hyphens and single quotes only)"
        if pattern.startswith("^[a-z]{2,3}"):
            return f"{path}: {error.instance!r} is not a language tag like en or en-GB"
    if error.validator == "additionalProperties":
        known = ", ".join(sorted(sub.get("properties", {})))
        return f"{path}: {error.message}; the fields here are: {known}"
    return f"{path}: {error.message}"


def schema_errors(data: Any) -> List[str]:
    """Every schema violation in a parsed kit, as 'field.path: what is wrong' lines (empty when it is valid)."""
    errors = sorted(_validator().iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])
    return [_friendly(e) for e in errors]


def _deep_merge(base: Dict[str, Any], over: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _palettes(colors: Mapping[str, Any]) -> Tuple[Palette, Palette]:
    """Resolve light and dark palettes: the kit's values, the neutral default for missing light roles, and derived values."""
    base = DEFAULT_KIT["colors"]
    given = {k: normalize_color(v) for k, v in colors.items() if k in COLOR_ROLES}
    light = {k: given.get(k, normalize_color(base[k])) for k in COLOR_ROLES if k != "link"}
    if "link" in given:
        light["link"] = given["link"]
    elif not given:
        light["link"] = base["link"]
    else:  # the button color when it reads as a link on the card and the background, otherwise the text color
        light["link"] = light["primary"] if _readable(light["primary"], light["card"], light["background"]) else light["text"]
    dgiven = {k: normalize_color(v) for k, v in (colors.get("dark") or {}).items()}
    if not given and not dgiven:
        dark = {k: normalize_color(v) for k, v in base["dark"].items()}
    else:
        dark = {
            "primary": dgiven.get("primary", light["primary"]),
            "button_text": dgiven.get("button_text", light["button_text"]),
            "text": dgiven.get("text", DARK_FALLBACK["text"]),
            "background": dgiven.get("background", DARK_FALLBACK["background"]),
            "card": dgiven.get("card", DARK_FALLBACK["card"]),
        }
        dark["link"] = dgiven.get("link") or (light["link"] if _readable(light["link"], dark["card"], dark["background"]) else dark["text"])
    return Palette(**light), Palette(**dark)


def _build(data: Mapping[str, Any], source: Optional[str]) -> BrandKit:
    merged = _deep_merge(DEFAULT_KIT, {k: v for k, v in data.items() if k != "colors"})
    light, dark = _palettes(data.get("colors") or {})
    fonts = merged.get("fonts") or {}
    web = fonts.get("web_font") or {}
    logo = merged.get("logo")
    footer = merged.get("footer") or {}
    voice = merged.get("voice") or {}
    return BrandKit(
        brand_name=str(merged.get("brand_name") or "").strip(),
        lang=merged.get("lang") or "en",
        light=light,
        dark=dark,
        fonts=Fonts(stack=" ".join(fonts.get("stack", DEFAULT_KIT["fonts"]["stack"]).split()),
                    web_font_name=web.get("name"), web_font_url=web.get("url")),
        website=merged.get("website"),
        logo=Logo(url=logo["url"], alt=logo["alt"].strip(), width=int(logo["width"]), height=logo.get("height")) if logo else None,
        footer=Footer(legal_name=footer.get("legal_name"), postal_address=footer.get("postal_address"),
                      extra_lines=tuple(footer.get("extra_lines") or ())),
        voice=Voice(register=voice.get("register"),
                    vocabulary=tuple(VocabularySwap(x["avoid"], x["use_instead"]) for x in voice.get("vocabulary") or ()),
                    forbidden_phrases=tuple(voice.get("forbidden_phrases") or ()), sign_off=voice.get("sign_off")),
        senders={role: Sender(s["from_name"], s["from_address"], s.get("reply_to")) for role, s in (merged.get("senders") or {}).items()},
        social=tuple(SocialLink(s["label"], s["url"]) for s in merged.get("social") or ()),
        source=source,
    )


def parse_brand(data: Any, source: str = "brand kit") -> BrandKit:
    """A kit from parsed YAML or JSON: schema check, defaults and derived colors, then the WCAG contrast check."""
    if not isinstance(data, Mapping):
        raise BrandKitError(source, ["a brand kit is a set of 'key: value' lines with at least brand_name (see templates/brand/brand.yaml)"])
    errors = schema_errors(data)
    if errors:
        raise BrandKitError(source, errors)
    kit = _build(data, source)
    failures = [c.failure() for c in kit.contrast_report() if c.enforced and not c.ok]
    if failures:
        raise BrandKitError(source, failures)
    return kit


def load_brand(path_or_none: Union[str, Path, None] = None) -> BrandKit:
    """The kit in a brand.yaml file, or the neutral default when given None."""
    if path_or_none is None:
        return DEFAULT_BRAND
    path = Path(path_or_none).expanduser()
    if not path.is_file() and not path.is_absolute():
        from engine.paths import ROOT
        if (ROOT / path).is_file():          # a demo brand's kit, named relative to the repository
            path = ROOT / path
    if not path.is_file():
        raise BrandKitError(str(path), ["no such file; copy templates/brand/brand.yaml to start one"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise BrandKitError(str(path), [f"not valid YAML{where}: {getattr(e, 'problem', None) or e}"]) from None
    return parse_brand(data, str(path))


def brand_path(workspace: Any = None) -> Optional[Path]:
    """Where a workspace's kit lives: lmm.yaml's brand_kit (relative to the workspace), else brand.yaml, else None."""
    from engine import workspace as ws_mod
    ws = workspace if workspace is not None else ws_mod.find()
    named = ws.config().get(WORKSPACE_KEY)
    if named:
        p = Path(str(named)).expanduser()
        p = p if p.is_absolute() else ws.root / p
        if not p.is_file():
            raise BrandKitError(str(ws.config_path), [f"{WORKSPACE_KEY}: {named} does not exist (paths are relative to {ws.root})"])
        return p
    p = ws.root / BRAND_FILE
    return p if p.is_file() else None


def find_brand(workspace: Any = None) -> BrandKit:
    """The kit for a workspace (engine.workspace.find() when none is given), or the neutral default when it has none."""
    return load_brand(brand_path(workspace))


DEFAULT_BRAND: BrandKit = _build(DEFAULT_KIT, None)


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a brand kit and print its WCAG contrast table")
    ap.add_argument("path", nargs="?", help="brand.yaml (default: the current workspace's kit, or the neutral default)")
    ap.add_argument("--json", action="store_true", help="print the resolved kit and contrast rows as JSON")
    a = ap.parse_args(argv)
    try:
        kit = load_brand(a.path) if a.path else find_brand()
    except BrandKitError as e:
        print(str(e))
        return 1
    rows = kit.contrast_report()
    if a.json:
        print(json.dumps({"kit": kit.as_dict(), "contrast": [asdict(r) | {"ok": r.ok} for r in rows]}, indent=2))
        return 0
    print(f"brand kit: {kit.source or 'neutral default'} ({kit.brand_name or 'uses the product name'})")
    for r in rows:
        print("  " + r.describe())
    print("OK: every text color reaches WCAG 2.1 AA (4.5:1)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
