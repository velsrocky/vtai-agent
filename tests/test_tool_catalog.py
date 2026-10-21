"""Generated catalog discipline: docs/tool-catalog.md must be reproduce-
able from the registry. A tool whose schema changed without the doc is a
drift failure (REA-style mcp-catalog:check behavior)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_tool_catalog", ROOT / "scripts" / "generate_tool_catalog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_catalog_matches_registry():
    committed = (ROOT / "docs" / "tool-catalog.md").read_text(encoding="utf-8")
    assert committed == _generator().build_catalog(), (
        "docs/tool-catalog.md is stale — re-run: "
        "uv run python scripts/generate_tool_catalog.py")


def test_catalog_lists_every_tool_once():
    from vtai_agent.mcp_server import register_tools
    from vtai_agent.tools.registry import registry

    register_tools()
    text = (ROOT / "docs" / "tool-catalog.md").read_text(encoding="utf-8")
    for name in registry.all():
        assert text.count(f"## `{name}`\n") == 1
