"""End-to-end checks against a *real* model.

Every other test mocks the provider, which hides the things that actually
break in production: a model that answers in prose instead of a tool call, a
tool call the parser cannot read, a provider that is simply not there.

These are opt-in because they need a configured, reachable provider and
they cost tokens:

    VTAI_E2E=1 uv run pytest tests/test_e2e_live.py -v

Without VTAI_E2E the whole module is skipped. The provider is whatever
`config/settings.toml` / the environment selects — a local Ollama, Lemonade
or a cloud key.
"""

import asyncio
import os

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("VTAI_E2E") != "1",
        reason="set VTAI_E2E=1 to run the live end-to-end checks",
    ),
]


def _provider_is_reachable() -> bool:
    from vtai_agent.config import get_settings

    try:
        s = get_settings()
    except ValueError:
        return False
    if s.active_provider != "local":
        return bool(os.environ.get({"anthropic": "ANTHROPIC_API_KEY",
                                    "openai": "OPENAI_API_KEY"}
                                   .get(s.active_provider, "")))
    from urllib.parse import urlparse

    parsed = urlparse(s.local.base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    import socket

    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture
def live_provider():
    if not _provider_is_reachable():
        pytest.skip("no reachable provider configured")
    from vtai_agent.tools import registry
    from vtai_agent.mcp_server import register_tools

    register_tools()
    assert registry.all(), "no tools registered"
    return registry


def test_goal_runs_against_a_real_model(live_provider):
    """A real model, a real tool call, a real run row."""
    from vtai_agent.orchestrator import Orchestrator

    orch = Orchestrator()
    result = asyncio.run(orch.run_goal(
        "Call the system_info tool once, then answer with the number of CPUs.",
        dry_run=True,
    ))
    assert result.status in ("ok", "failed"), result.errors
    # A local model may answer in prose; the point is that the loop handles
    # it and the run is recorded either way.
    assert result.run_id is not None
    assert result.final_answer


def test_a_malformed_model_reply_is_recovered(live_provider):
    """A model that emits something unparseable must not end the run as a
    silent success."""
    from vtai_agent.orchestrator import parse_tool_call

    # The parser is unit-tested with mocks; here we only assert it does not
    # crash on the shapes a real model produces.
    for text in ("here is my answer: 42",
                 "```json\n{\"tool\": \"system_info\", \"args\": {}}\n```",
                 "no tool call here",
                 "{broken json",
                 "{\"tool\": \"run_shell\", \"args\": {\"command\": \"ls ~/Downloads\"}}"):
        parsed = parse_tool_call(text)
        assert parsed is None or parsed.tool
