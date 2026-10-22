"""
Repository check: the engine's own examples must meet the syntax rules it imposes on Claude.

Walks every Markdown, SQL, JSON, XML and HTML file and fails on:
  * a fenced ```sql <dialect>``` block that does not parse in its dialect, or a PostgreSQL SELECT
    block that does not run on the DuckDB fixtures
  * a fenced template block (```liquid braze```, ```liquid customerio```, ```django klaviyo```,
    ```hubl```, ```ampscript```, ```handlebars```) with a syntax error in its dialect
  * a ```json``` block or .json file that does not load; a JSON Schema that is not a valid 2020-12 schema
  * an .xml file or ```xml``` block that is not well formed
  * an HTML email template that fails the template linter in its declared dialect
  * any em dash (U+2014) anywhere: the house style forbids them

    python3 engine/repo_check.py            # exit 1 on any failure
    python3 engine/repo_check.py --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.sql import check as sql_check  # noqa: E402
from engine.render import template_lint  # noqa: E402

EM_DASH = chr(0x2014)  # written as a code point so this file passes its own check
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "out"}
TEXT_SUFFIXES = {".md", ".sql", ".json", ".xml", ".html", ".py", ".yml", ".yaml", ".txt", ".toml", ".cfg", ".csv", ".mjml"}
FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```\s*$", re.S | re.M)
TEMPLATE_FENCES = {("liquid", "braze"): "braze", ("liquid", "customerio"): "customerio", ("liquid", "customerio_legacy"): "customerio_legacy",
                   ("liquid", ""): "liquid", ("django", "klaviyo"): "klaviyo", ("hubl", ""): "hubl", ("jinja", "hubl"): "hubl",
                   ("ampscript", ""): "ampscript", ("handlebars", ""): "handlebars", ("handlebars", "iterable"): "handlebars"}


def files() -> List[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            out.append(p)
    return sorted(out)


def check(verbose: bool = False) -> Tuple[List[str], Dict[str, int]]:
    errors: List[str] = []
    counts = {"sql_parsed": 0, "sql_executed": 0, "templates": 0, "json": 0, "schemas": 0, "xml": 0, "html": 0, "files_scanned": 0}
    con = None

    def run_sql(code: str, where: str) -> None:
        nonlocal con
        if con is None:
            con = sql_check.connect()
        try:
            _cols, rows = sql_check.run(code, con)
            counts["sql_executed"] += 1
            if verbose:
                print(f"  ran {where}: {len(rows)} rows")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{where}: PostgreSQL block failed on DuckDB fixtures: {str(e).splitlines()[0]}")

    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            counts["files_scanned"] += 1
            for n, line in enumerate(text.splitlines(), 1):
                if EM_DASH in line:
                    errors.append(f"{rel}:{n}: em dash")
        else:
            continue

        if path.suffix == ".md":
            for m in FENCE.finditer(text):
                info = m.group(1).strip().split()
                lang = info[0].lower() if info else ""
                tag = info[1].lower() if len(info) > 1 else ""
                code = m.group(2)
                line = text[: m.start()].count("\n") + 1
                where = f"{rel}:{line}"
                if lang == "sql":
                    dialect = sql_check.DIALECT_ALIASES.get(tag) or sql_check.declared_dialect(code)
                    if not dialect:
                        errors.append(f"{where}: sql block without a dialect (```sql postgres|snowflake|bigquery)")
                        continue
                    ok, msg = sql_check.parse(code, dialect)
                    counts["sql_parsed"] += 1
                    if not ok:
                        errors.append(f"{where}: sql ({dialect}) does not parse: {msg}")
                    elif dialect == "postgres" and re.match(r"\s*(--[^\n]*\n\s*)*(WITH|SELECT)\b", code, re.I) and "-- no-run" not in code:
                        run_sql(code, where)
                elif (lang, tag) in TEMPLATE_FENCES or (lang, "") in TEMPLATE_FENCES and lang in ("hubl", "ampscript", "handlebars"):
                    dialect = TEMPLATE_FENCES.get((lang, tag)) or TEMPLATE_FENCES[(lang, "")]
                    r = template_lint.lint(code, dialect)
                    counts["templates"] += 1
                    for e in r["errors"]:
                        errors.append(f"{where}: {dialect} template: {e}")
                elif lang == "json":
                    try:
                        json.loads(code)
                        counts["json"] += 1
                    except json.JSONDecodeError as e:
                        errors.append(f"{where}: json block does not load: {e}")
                elif lang == "xml":
                    try:
                        ET.fromstring(code)
                        counts["xml"] += 1
                    except ET.ParseError as e:
                        errors.append(f"{where}: xml block is not well formed: {e}")
        elif path.suffix == ".sql":
            dialect = sql_check.declared_dialect(text)
            if not dialect:
                errors.append(f"{rel}: .sql file without a '-- dialect:' header")
                continue
            ok, msg = sql_check.parse(text, dialect)
            counts["sql_parsed"] += 1
            if not ok:
                errors.append(f"{rel}: does not parse as {dialect}: {msg}")
            elif dialect == "postgres" and "-- no-run" not in text:
                run_sql(text, rel)
        elif path.suffix == ".json":
            try:
                doc = json.loads(text)
                counts["json"] += 1
            except json.JSONDecodeError as e:
                errors.append(f"{rel}: does not load: {e}")
                continue
            if isinstance(doc, dict) and "$schema" in doc and "json-schema.org" in str(doc["$schema"]):
                import jsonschema
                try:
                    jsonschema.Draft202012Validator.check_schema(doc)
                    counts["schemas"] += 1
                except jsonschema.SchemaError as e:
                    errors.append(f"{rel}: not a valid JSON Schema 2020-12: {e.message}")
        elif path.suffix == ".xml":
            try:
                ET.parse(path)
                counts["xml"] += 1
            except ET.ParseError as e:
                errors.append(f"{rel}: not well formed: {e}")
        elif path.suffix == ".html":
            m = re.search(r"<!--\s*dialect:\s*([a-z_]+)", text)
            if not m:
                errors.append(f"{rel}: HTML template without a '<!-- dialect: ... -->' comment")
                continue
            r = template_lint.lint(text, m.group(1), email=True)
            counts["html"] += 1
            for e in r["errors"]:
                errors.append(f"{rel}: {e}")
            if verbose:
                for w in r["warnings"]:
                    print(f"  warning {rel}: {w}")
    return errors, counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcm repo-check", description="Check every example in the repository against its own syntax rules")
    ap.add_argument("--verbose", action="store_true", help="also print warnings from the HTML template lint, not only failures")
    a = ap.parse_args(argv)
    errors, counts = check(a.verbose)
    print("repo check: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for e in errors:
        print("  FAIL " + e)
    print("OK" if not errors else f"{len(errors)} failure(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
