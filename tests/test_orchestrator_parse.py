"""Tool-call parsing and the malformed-call recovery path.

The orchestrator must never report a broken JSON tool call as a successful
final answer, and ordinary code answers (plain fences) must not be mistaken
for malformed calls."""

import asyncio
import os
import platform

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import pytest
from sqlmodel import select

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from vtai_agent import db
from vtai_agent.config import Guardrails as GuardrailsConfig, ProviderOpenAI, Settings
from vtai_agent.mcp_server import register_tools
from vtai_agent.models import Step
from vtai_agent.orchestrator import (
    Orchestrator,
    looks_like_tool_call,
    parse_tool_call,
)
from vtai_agent.tools import registry


def _orch_settings(sandbox) -> Settings:
    return Settings(
        active_provider="openai",
        openai=ProviderOpenAI(model="test", cost_per_1k_input=1.0, cost_per_1k_output=1.0),
        guardrails=GuardrailsConfig(
            dry_run_default=True,
            max_steps=6,
            max_retries=1,
            budget_usd_per_run=10.0,
            deny_globs=sandbox.cfg.deny_globs,
            trusted_bin_dirs=sandbox.cfg.trusted_bin_dirs,
        ),
        paths=sandbox.paths,
    )


# ---------- parse_tool_call ----------
def test_fenced_call_with_prose_around_it_parses():
    text = ("Let me check the system.\n"
            '```json\n{"tool": "run_shell", "args": {"command": "uptime"}}\n```\n'
            "One moment.")
    call = parse_tool_call(text)
    assert call is not None and call.tool == "run_shell"
    assert call.args == {"command": "uptime"}


def test_bare_call_with_nested_args_is_not_overcaptured():
    text = '{"tool": "t", "args": {"command": "ls", "note": "{not json}"}} hope this works'
    call = parse_tool_call(text)
    assert call is not None and call.tool == "t"
    assert call.args["command"] == "ls"


def test_plain_answer_is_not_a_tool_call():
    assert parse_tool_call("All done: nothing changed.") is None


# ---------- looks_like_tool_call ----------
def test_broken_json_with_tool_key_looks_like_a_call():
    assert looks_like_tool_call('{"tool": "run_shell", "command":}')
    assert looks_like_tool_call('{"tool" "run_shell"}')


def test_json_fenced_block_looks_like_a_call():
    assert looks_like_tool_call("```json\n{'tool': 'run_shell'}\n```")


def test_plain_code_answer_is_not_malformed():
    assert not looks_like_tool_call("Here is the patch:\n```\nprint(1)\n```")
    assert not looks_like_tool_call("```python\nx = 'tool'\n```")
    assert not looks_like_tool_call("All finished, no changes needed.")


# ---------- orchestrator loop recovery ----------
def _scripted_model(outputs: list[str]):
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        return ModelResponse(parts=[TextPart(outputs[state["n"] - 1])])

    return FunctionModel(fn)


def _steps(run_id: int) -> list[str]:
    with db.session_scope() as s:
        return [st.tool for st in s.exec(select(Step).where(Step.run_id == run_id)).all()]


@pytest.fixture
def tools(sandbox):
    registry.clear()
    register_tools()
    yield


def test_orchestrator_recovers_from_malformed_call(sandbox, tools):
    broken = "```json\n{'tool': 'run_shell', 'args': {}}\n```"
    good = '{"tool": "run_shell", "args": {"command": "uptime", "dry_run": true}}'
    orch = Orchestrator(settings=_orch_settings(sandbox),
                        model=_scripted_model([broken, good, "all done"]))
    res = asyncio.run(orch.run_goal("tidy"))
    assert res.status == "ok"
    assert res.final_answer == "all done"
    tools_run = _steps(res.run_id)
    assert "malformed_call" in tools_run
    assert "run_shell" in tools_run
    assert any("malformed tool call" in e for e in res.errors)


def test_orchestrator_code_answer_is_final_not_malformed(sandbox, tools):
    answer = "Here is the patch:\n```\nprint(1)\n```"
    orch = Orchestrator(settings=_orch_settings(sandbox),
                        model=_scripted_model([answer]))
    res = asyncio.run(orch.run_goal("show code"))
    assert res.status == "ok"
    assert res.final_answer == answer
    assert "malformed_call" not in _steps(res.run_id)


def test_persistent_malformed_calls_fail_the_run_not_ok(sandbox):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart('{"tool" "run_shell"}, broken')])

    orch = Orchestrator(settings=_orch_settings(sandbox), model=FunctionModel(fn))
    res = asyncio.run(orch.run_goal("noop"))
    # Bounded retries with the corrective note, then a failed run — never a
    # success status carrying unparsed garbage as the answer.
    assert res.status == "failed"
    assert "max_steps" in res.final_answer or all(
        "malformed" in e for e in res.errors)


# ---------- system prompt is platform-aware ----------
def test_system_prompt_names_this_platform():
    from vtai_agent.orchestrator import _MACHINE_OS, _SYSTEM
    # _MACHINE_OS humanizes the raw platform token: macOS reports "Darwin",
    # but the prompt (and users) say "macOS". Assert the mapping, not the
    # raw system() value — that equality only holds on Windows/Linux.
    expected = {"Windows": "Windows", "Linux": "Linux",
                "Darwin": "macOS"}.get(platform.system(), platform.system())
    assert _MACHINE_OS == expected
    prompt = _SYSTEM.format(manifest="", mode="x", machine_os=_MACHINE_OS)
    assert f"this {expected} machine" in prompt
