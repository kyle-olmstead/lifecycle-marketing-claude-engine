import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```\s*$", re.S | re.M)


def fenced(path: Path, lang: str, tag: str = None):
    """Every fenced block in a Markdown file with the given language (and optional tag)."""
    out = []
    for m in FENCE.finditer(path.read_text(encoding="utf-8")):
        info = m.group(1).split()
        if info and info[0] == lang and (tag is None or (len(info) > 1 and info[1] == tag)):
            out.append(m.group(2))
    return out


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def duck():
    from engine.sql import check as sql_check
    return sql_check.connect()


@pytest.fixture(scope="session")
def incident():
    return json.loads((ROOT / "examples/deliverability/incident.json").read_text())
