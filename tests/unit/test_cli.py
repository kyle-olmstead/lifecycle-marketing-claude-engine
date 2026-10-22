"""lcm: every subcommand dispatches, every flag explains itself, and a change to the last plan is a revision."""
import contextlib
import io
import re

import pytest

from engine.cli import main as cli
from engine.cli.commands import COMMANDS, STATS_SUBS

SKIP_HELP = {"mcp"}          # starts a server; its own help is checked below


def _help(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def _undocumented(text):
    """Option and positional entries in an argparse help screen that carry no description."""
    lines = text.splitlines()
    bad, section = [], None
    for i, line in enumerate(lines):
        if re.match(r"^(options|optional arguments|positional arguments)", line) or re.match(r"^\S.*:$", line):
            section = line
            continue
        if section is None or not line.strip():
            continue
        m = re.match(r"^( {2}| {4})(\S.*)$", line)
        if not m:
            continue
        body = m.group(2)
        if body.startswith("{"):          # the list of subcommands; each one is checked on the next lines
            continue
        has_text = re.search(r"\S\s{2,}\S", body) is not None
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        continues = re.match(r"^ {6,}\S", nxt) is not None and len(nxt) - len(nxt.lstrip()) > len(m.group(1)) + 2
        if not has_text and not continues:
            bad.append(body.split()[0])
    return bad


def _subcommands(text):
    """The subcommands listed under 'positional arguments' (a {a,b} line of its own), not a flag's choices."""
    pos = text.split("positional arguments:", 1)
    if len(pos) < 2:
        return []
    m = re.search(r"^  \{([\w,-]+)\}", pos[1], re.M)
    return m.group(1).split(",") if m else []


@pytest.mark.parametrize("name", sorted(set(COMMANDS) - SKIP_HELP))
def test_every_flag_of_every_command_has_help(name):
    rc, text = _help([name, "--help"])
    assert rc in (0, None), (name, rc, text[:300])
    assert text.strip(), name
    problems = [f"{name}: {x}" for x in _undocumented(text)]
    from engine.cli.commands import DELIVERABILITY
    for sub in _subcommands(text) + (list(DELIVERABILITY) if name == "deliverability" else []):
        _, sub_text = _help([name, sub, "--help"])
        problems += [f"{name} {sub}: {x}" for x in _undocumented(sub_text)]
    assert not problems, problems


def test_overview_lists_every_command():
    rc, text = _help(["--help"])
    assert rc == 0
    for name in COMMANDS:
        assert re.search(rf"^  {re.escape(name)}\s", text, re.M), name


def test_mcp_help_does_not_start_a_server():
    rc, text = _help(["mcp", "--help"])
    assert rc == 0 and "stdio" in text


def test_size_reads_like_a_sentence():
    rc, text = _help(["size", "--baseline", "0.1", "--mde-rel", "0.2", "--holdout", "0.15"])
    assert rc == 0 and "To detect a move from 10.0% to 12.0%" in text and "{" not in text
    rc, text = _help(["size", "--baseline", "0.1", "--mde-rel", "0.2", "--holdout", "0.15", "--entries-per-month", "2000"])
    assert "reads     in about" in text and "Holdout  Months to read" in text
    rc, text = _help(["size", "--baseline", "0.1", "--mde-rel", "0.2", "--json"])
    assert text.lstrip().startswith("{")
    assert set(STATS_SUBS) >= {"size", "mde", "feasible"}


def test_readout_from_counts_says_whether_it_could_be_chance():
    rc, text = _help(["readout", "--xt", "400", "--nt", "3000", "--xc", "50", "--nc", "500"])
    assert "p = 0.039" in text and "unlikely to be chance" in text
    rc, text = _help(["readout", "--xt", "4", "--nt", "30", "--xc", "1", "--nc", "5"])
    assert "not readable" in text


def test_value_per_conversion_is_an_alias():
    job = cli.route("what should we build next", {"data": "d", "value": 80.0})
    assert job.args["value"] == 80.0
    rc, text = _help(["what should we build next", "--data", "d", "--value-per-conversion", "80", "--dry-run"])
    assert '"value": 80.0' in text


@pytest.mark.parametrize("sentence,changes", [
    ("make it 4 touches", {"touches": 4}),
    ("Now make it five messages", {"touches": 5}),
    ("use a 20% holdout", {"holdout_pct": 20.0}),
    ("switch to Klaviyo", {"platform": "klaviyo"}),
])
def test_a_change_to_the_last_plan_is_a_revision(sentence, changes, tmp_path, monkeypatch):
    run = tmp_path / "out" / "acme_winback" / "runs" / "abc123"
    run.mkdir(parents=True)
    (run / "state.json").write_text('{"brief": {"flow_type": "winback", "channels": ["email"]}}')
    monkeypatch.setenv("LCM_WORKSPACE", str(tmp_path / "nowhere"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "newest_run", lambda: str(run))
    assert cli.classify(sentence) == "revise"
    job = cli.route(sentence, {})
    assert job.kind == "revise" and job.args["changes"] == changes and job.args["run"] == str(run)


def test_channel_edits_start_from_the_runs_own_channels(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    (run / "state.json").write_text('{"brief": {"flow_type": "winback", "channels": ["email", "push"]}}')
    assert cli.revision("add SMS", str(run))["channels"] == ["email", "push", "sms"]
    assert cli.revision("remove push", str(run))["channels"] == ["email"]
    assert cli.revision("make the wait 30 days", str(run))["churn_threshold_days"] == 30


def test_new_flows_are_still_plans():
    for s in ("set up a welcome flow", "build a 3-touch abandoned cart flow", "plan a 5-touch win-back for users inactive 14 days"):
        assert cli.classify(s) == "plan", s


def test_revise_with_no_run_asks_for_one(monkeypatch):
    monkeypatch.setattr(cli, "newest_run", lambda: None)
    job = cli.route("make it 4 touches", {})
    assert job.kind == "revise" and job.missing and job.missing[0].startswith("--run")
