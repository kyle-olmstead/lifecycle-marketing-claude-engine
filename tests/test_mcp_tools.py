"""The MCP server's v3 tools answer on the demo workspaces."""
import asyncio

import pytest

from tests.conftest import ROOT

pytest.importorskip("mcp")


def _call(name, args):
    from engine import mcp_server
    return asyncio.run(mcp_server.server.call_tool(name, args)).content[0].text


def test_check_data_and_import(tmp_path):
    assert "PASS" in _call("check_data", {"data_dir": str(ROOT / "examples/brands/northfield_dtc/data")})
    out = _call("import_exports", {"source": "auto", "folders": [str(ROOT / "examples/brands/northfield_dtc/exports")],
                                   "out_dir": str(tmp_path / "data")})
    assert "# Import: shopify + klaviyo" in out and (tmp_path / "data" / "orders.csv").exists()


def test_program_size_and_doctor(monkeypatch):
    monkeypatch.setenv("LCM_WORKSPACE", str(ROOT / "examples/brands/northfield_dtc"))
    assert "Program simulation (2 flows)" in _call("simulate_program", {})
    assert "To detect a move from 10.0% to 12.0%" in _call("size_holdout", {"baseline": 0.1, "mde_rel": 0.2})
    assert "reads     in about" in _call("size_holdout", {"baseline": 0.1, "mde_rel": 0.2, "entries_per_month": 2000})
    doc = _call("workspace_doctor", {})
    assert "lmm.yaml: valid" in doc and "FAIL" not in doc
