"""
Context budget: how many tokens a typical invocation of each skill puts in front of Claude.

    python3 -m engine.context_budget                 # the working tree, against tests/fixtures/context_baseline.json
    python3 -m engine.context_budget --rev 7f3fe69   # any commit, every file read with `git show REV:<path>`
    python3 -m engine.context_budget --json          # the same numbers as JSON (how the baseline fixture was written)

The typical load of a skill is

    the SKILL.md itself
  + every file the skill marks always-read (read on every invocation)
  + the mean size of the job prompts it routes to (one job per invocation)

Tokens are approximated as characters / 4 (rounded). It is the usual rule of thumb for English prose and
code; the real tokenizer differs by a few percent either way, the same way for the baseline and for now,
so the change between them is what to read.

Which references count
  Only `${CLAUDE_PLUGIN_ROOT}/<path>` references in the SKILL.md text, and only documents Claude reads:
  .md, .xml, .yaml, .yml, .json, .html and .sql files. Scripts are run, not read, and requirements.txt is
  installed, not read. Files in the user's workspace (lmm.yaml, lmm-engine/context.xml) are not in the
  repository and are not counted. A reference to a directory names a place, not a read, and is skipped.

How a reference is classified (a block is one paragraph or one list item)
  Marker convention (v3 skills):
    * always-read: every document path in a block whose text starts with "Always read" (or in the list
      items that follow such a line).
    * routed: every document path in the list items that follow a line starting with "Route", up to the
      next heading or the next paragraph that is not a list item.
    * everything else is read on demand (only when the task needs it) and is not counted.
  Legacy skills (no "Always read" or "Route" marker anywhere; every v2 skill):
    * always-read: document paths in blocks that start with "Read" inside the first "## " section, which
      is the load step every v2 skill opens with ("Step 0: Load context and rules").
    * routed: document paths in the list items under a heading containing "Pick the module".
  Per-dialect reference SQL: when a routed job prompt <dir>/<job>.md has a folder <dir>/<job>/ holding
  <job>_postgres.sql, <job>_snowflake.sql or <job>_bigquery.sql, one of them is read per invocation (the
  workspace's warehouse dialect), so the mean of their sizes is added to that job's size.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "fixtures" / "context_baseline.json"
DOC_SUFFIXES = {".md", ".xml", ".yaml", ".yml", ".json", ".html", ".sql"}
DIALECTS = ("postgres", "snowflake", "bigquery")
PATH_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
HEADING = re.compile(r"^#{1,6}\s")
MARKUP = re.compile(r"^[\s>*_`\-+\d.]*")


def tokens(chars: int) -> int:
    """Characters / 4, the approximation every number in this module uses."""
    return round(chars / 4)


class Source:
    """Reads repository files from the working tree, or from a commit with `git show REV:<path>`."""

    def __init__(self, rev: Optional[str] = None, root: Path = ROOT):
        self.rev, self.root = rev, root
        self._cache: Dict[str, Optional[str]] = {}
        self._tree: Optional[set] = None

    def files(self) -> set:
        if self._tree is None:
            if self.rev:
                out = subprocess.run(["git", "ls-tree", "-r", "--name-only", self.rev], cwd=self.root,
                                     capture_output=True, text=True, check=True).stdout
                self._tree = set(out.split())
            else:
                self._tree = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}
        return self._tree

    def is_dir(self, rel: str) -> bool:
        prefix = rel.rstrip("/") + "/"
        return any(f.startswith(prefix) for f in self.files())

    def exists(self, rel: str) -> bool:
        rel = rel.rstrip("/")
        return rel in self.files() or self.is_dir(rel)

    def read(self, rel: str) -> Optional[str]:
        if rel not in self._cache:
            if rel not in self.files():
                self._cache[rel] = None
            elif self.rev:
                r = subprocess.run(["git", "show", f"{self.rev}:{rel}"], cwd=self.root, capture_output=True)
                self._cache[rel] = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
            else:
                self._cache[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        return self._cache[rel]

    def chars(self, rel: str) -> int:
        text = self.read(rel)
        return len(text) if text is not None else 0


def referenced_paths(text: str) -> List[str]:
    """Every ${CLAUDE_PLUGIN_ROOT}/... path in a text, in order, without trailing punctuation."""
    return [m.group(1).rstrip(".,;:") for m in PATH_RE.finditer(text)]


def _blocks(text: str) -> List[Dict[str, object]]:
    """Paragraphs and list items, each with the heading it sits under, its section index (counting '## ' headings),
    whether it is a list item, and the first line of the paragraph that introduced its list ('' when none did)."""
    blocks: List[Dict[str, object]] = []
    heading, section, in_fence = "", 0, False
    current: Optional[Dict[str, object]] = None

    def close():
        nonlocal current
        if current is not None:
            blocks.append(current)
            current = None

    for line in text.splitlines():
        if in_fence or line.strip().startswith("```"):     # a fenced block belongs to the block it sits in
            if line.strip().startswith("```"):
                in_fence = not in_fence
            if current is not None:
                current["text"] = str(current["text"]) + "\n" + line
            continue
        if HEADING.match(line):
            close()
            heading = line.lstrip("#").strip()
            section += line.startswith("## ")
            continue
        if not line.strip():
            close()
            continue
        if LIST_ITEM.match(line):
            close()
            current = {"text": LIST_ITEM.sub("", line, count=1), "heading": heading, "section": section, "item": True}
            continue
        if current is not None:
            current["text"] = str(current["text"]) + "\n" + line
            continue
        current = {"text": line, "heading": heading, "section": section, "item": False}
    close()
    # a list's lead-in is the paragraph just before it; a heading or a later paragraph ends the list
    lead, under = "", None
    for b in blocks:
        if b["heading"] != under:
            lead, under = "", b["heading"]
        if b["item"]:
            b["lead"] = lead
        else:
            b["lead"], lead = "", str(b["text"]).splitlines()[0]
    return blocks


def _starts(text: str, word: str) -> bool:
    return MARKUP.sub("", text, count=1).lower().startswith(word.lower())


def classify(skill_text: str) -> Dict[str, object]:
    """{'mode': 'marker'|'legacy', 'always': [...], 'routed': [...], 'on_demand': [...]} for one SKILL.md text."""
    blocks = _blocks(skill_text)
    marker = any(_starts(str(b["text"]), "Always read") for b in blocks) or \
        any(not b["item"] and _starts(str(b["text"]), "Route") for b in blocks)
    always: List[str] = []
    routed: List[str] = []
    for b in blocks:
        text = str(b["text"])
        paths = referenced_paths(text)
        if not paths:
            continue
        if marker:
            if _starts(text, "Always read") or (b["item"] and _starts(str(b["lead"]), "Always read")):
                always += paths
            elif b["item"] and _starts(str(b["lead"]), "Route"):
                routed += paths
        else:
            if b["section"] == 1 and _starts(text, "Read"):
                always += paths
            elif b["item"] and "pick the module" in str(b["heading"]).lower():
                routed += paths
    every = referenced_paths(skill_text)

    def docs(paths: List[str]) -> List[str]:
        seen: List[str] = []
        for p in paths:
            if Path(p).suffix.lower() in DOC_SUFFIXES and p not in seen:
                seen.append(p)
        return seen

    always_d, routed_d = docs(always), [p for p in docs(routed) if p not in always]
    on_demand = [p for p in docs(every) if p not in always_d and p not in routed_d]
    return {"mode": "marker" if marker else "legacy", "always": always_d, "routed": routed_d, "on_demand": on_demand}


def dialect_files(src: Source, job: str) -> List[str]:
    """The per-dialect reference SQL a job prompt points to: <dir>/<job>/<job>_<dialect>.sql."""
    p = Path(job)
    if p.suffix != ".md":
        return []
    folder = p.with_suffix("")
    return [f"{folder.as_posix()}/{p.stem}_{d}.sql" for d in DIALECTS if f"{folder.as_posix()}/{p.stem}_{d}.sql" in src.files()]


def job_chars(src: Source, job: str) -> Dict[str, object]:
    sql = dialect_files(src, job)
    extra = mean(src.chars(f) for f in sql) if sql else 0
    return {"path": job, "chars": src.chars(job), "dialect_files": sql, "dialect_mean_chars": round(extra),
            "total_chars": round(src.chars(job) + extra)}


def skill_budget(src: Source, skill_rel: str) -> Dict[str, object]:
    text = src.read(skill_rel) or ""
    c = classify(text)
    always = [{"path": p, "chars": src.chars(p)} for p in c["always"] if p in src.files()]
    jobs = [job_chars(src, p) for p in c["routed"] if p in src.files()]
    always_chars = sum(a["chars"] for a in always)
    routed_mean = round(mean(j["total_chars"] for j in jobs)) if jobs else 0
    total = len(text) + always_chars + routed_mean
    return {
        "skill": skill_rel.split("/")[1],
        "mode": c["mode"],
        "skill_tokens": tokens(len(text)),
        "always_read_tokens": tokens(always_chars),
        "routed_mean_tokens": tokens(routed_mean),
        "total_tokens": tokens(total),
        "always_read": always,
        "routed": jobs,
        "on_demand": c["on_demand"],
        "missing": [p for p in referenced_paths(text) if not src.exists(p)],
    }


def budget(rev: Optional[str] = None, root: Path = ROOT) -> Dict[str, object]:
    src = Source(rev, root)
    skills = sorted(f for f in src.files() if re.fullmatch(r"skills/[^/]+/SKILL\.md", f))
    rows = [skill_budget(src, s) for s in skills]
    return {"rev": rev or "working tree", "method": "tokens = characters / 4; typical load = SKILL.md + always-read files + "
            "mean routed job prompt (plus the mean per-dialect reference SQL it points to)",
            "skills": {r["skill"]: r for r in rows},
            "total_tokens": sum(r["total_tokens"] for r in rows)}


def load_baseline(path: Path = BASELINE) -> Optional[Dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def table(now: Dict[str, object], base: Optional[Dict[str, object]]) -> str:
    head = f"{'skill':<22} {'SKILL.md':>8} {'always':>7} {'routed':>7} {'total':>7}"
    if base:
        head += f" {'baseline':>9} {'change':>7}"
    lines = [f"context budget, {now['rev']} (tokens = characters / 4)", head, "-" * len(head)]
    for name, r in now["skills"].items():
        line = f"{name:<22} {r['skill_tokens']:>8,} {r['always_read_tokens']:>7,} {r['routed_mean_tokens']:>7,} {r['total_tokens']:>7,}"
        b = (base or {}).get("skills", {}).get(name)
        if base:
            line += f" {b['total_tokens']:>9,} {(r['total_tokens'] / b['total_tokens'] - 1) * 100:>+6.0f}%" if b else f" {'(new)':>9} {'':>7}"
        lines.append(line)
    lines.append("-" * len(head))
    total = f"{'all skills':<22} {'':>8} {'':>7} {'':>7} {now['total_tokens']:>7,}"
    if base:
        total += f" {base['total_tokens']:>9,} {(now['total_tokens'] / base['total_tokens'] - 1) * 100:>+6.0f}%"
    lines.append(total)
    if base:
        lines.append(f"baseline: {base['rev']} ({BASELINE.relative_to(ROOT).as_posix()})")
    missing = {n: r["missing"] for n, r in now["skills"].items() if r["missing"]}
    for n, m in missing.items():
        lines.append(f"  MISSING in {n}: {', '.join(m)}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tokens a typical invocation of each skill loads, against the v2.3 baseline")
    ap.add_argument("--rev", help="read every file at this git revision (default: the working tree)")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    ap.add_argument("--baseline", default=str(BASELINE), help="baseline JSON to compare against")
    a = ap.parse_args(argv)
    now = budget(a.rev)
    if a.json:
        print(json.dumps(now, indent=2))
        return 0
    print(table(now, load_baseline(Path(a.baseline))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
