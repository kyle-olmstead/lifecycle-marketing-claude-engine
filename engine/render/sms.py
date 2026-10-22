"""
SMS length, encoding and segment count (prompts/jobs/copy/sms_push_in_app.md).

Carriers bill and deliver SMS in segments. A message that is all GSM-7 characters fits 160 per
single segment and 153 per segment once concatenated. One character outside GSM-7 (a curly quote,
an em dash, an emoji) switches the whole message to UCS-2: 70 single, 67 per concatenated segment,
and emoji count as two units. This tool names the characters that forced the switch so they can be
replaced.

    python3 engine/render/sms.py "Northfield: your order shipped. Track it: https://nf.example.com/t/8KD2 Reply STOP to opt out."
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/render/sms.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional

GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = set("^{}\\[~]|€\f")  # each costs two septets
REPLACEMENTS = {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"', chr(0x2013): "-", chr(0x2014): "-", chr(0x2026): "...", chr(0x00A0): " "}
KEYWORDS = r"(STOP|STOPALL|UNSUBSCRIBE|END|QUIT|CANCEL|OPTOUT|REVOKE)"
STRONG = r"(STOP|STOPALL|UNSUBSCRIBE|OPTOUT)"
# An opt-out instruction, not any use of the word: "Reply STOP", "Text END to 12345", "STOP to opt out", "STOP=stop".
# "Stop by our store", "NON-STOP savings", "end to end encrypted" and "cancel to end your trial" are not instructions.
Q = "['\"\u201c\u201d\u2018\u2019]?"   # straight or curly quotes around the keyword
OPT_OUT = re.compile(r"\b(?:reply|replying|respond|responding|text|txt|send|msg)\s+(?:with\s+)?" + Q + KEYWORDS + r"\b"
                     r"|(?<![\w-])" + STRONG + Q + r"\s+(?:to|2)\s+(?:opt|unsub|stop|cancel|quit|end)"
                     r"|(?<![\w-])" + STRONG + r"\s*=\s*(?:stop|opt|unsub|cancel|end|quit)", re.I)


def has_opt_out(text: str) -> bool:
    return bool(OPT_OUT.search(text or ""))


def _segments(text: str, gsm: bool) -> int:
    """Segments as carriers pack them: a GSM-7 escape pair (2 septets) or a UTF-16 surrogate pair (2 units)
    is never split across segments, so a segment that cannot take the whole character closes early."""
    sizes = [2 if ch in GSM7_EXTENSION else 1 for ch in text] if gsm else [len(ch.encode("utf-16-le")) // 2 for ch in text]
    single, multi = (160, 153) if gsm else (70, 67)
    if sum(sizes) <= single:
        return 1
    count, used = 1, 0
    for n in sizes:
        if used + n > multi:
            count, used = count + 1, 0
        used += n
    return count


def analyze(text: str, brand: Optional[str] = None, first_message: bool = False) -> Dict[str, Any]:
    non_gsm = sorted({ch for ch in text if ch not in GSM7_BASIC and ch not in GSM7_EXTENSION})
    if not non_gsm:
        units = sum(2 if ch in GSM7_EXTENSION else 1 for ch in text)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        units = len(text.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    segments = _segments(text, encoding == "GSM-7")
    out: Dict[str, Any] = {
        "encoding": encoding, "units": units, "segments": segments,
        "limit_single": single, "limit_per_segment_concatenated": multi,
        "non_gsm_characters": [{"char": c, "codepoint": f"U+{ord(c):04X}", "replace_with": REPLACEMENTS.get(c)} for c in non_gsm],
        "checks": [],
    }
    if non_gsm and all(c in REPLACEMENTS for c in non_gsm):
        fixed = "".join(REPLACEMENTS.get(c, c) for c in text)
        alt = analyze(fixed)
        out["checks"].append(f"every non-GSM character has a plain replacement; with them the message is GSM-7, {alt['units']} units, {alt['segments']} segment(s)")
        out["gsm7_version"] = fixed
    if not OPT_OUT.search(text):
        out["checks"].append("no opt-out keyword: include 'Reply STOP to opt out' in the first message and periodically after (CTIA guidance)")
    if brand and brand.lower() not in text.lower():
        out["checks"].append(f"the sender is not identified: name '{brand}' in the message")
    if first_message and not re.search(r"\b(HELP)\b", text):
        out["checks"].append("first message: add 'Reply HELP for help' and message frequency per your 10DLC campaign registration")
    if re.search(r"https?://(bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly)/", text, re.I):
        out["checks"].append("public link shortener: carriers filter shared shortener domains; use a branded short domain")
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm sms", description="SMS encoding and segment count")
    ap.add_argument("text", help="the SMS message, in quotes")
    ap.add_argument("--brand", help="brand name the message should identify the sender with (checked if given)")
    ap.add_argument("--first-message", action="store_true", help="this is the first message: also check for HELP language")
    a = ap.parse_args(argv)
    print(json.dumps(analyze(a.text, a.brand, a.first_message), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
