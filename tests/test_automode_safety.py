"""auto_mode/delegate safety: bad CLI invocations and dead judges fail closed."""

import asyncio
import shutil

import pytest

from vtai_agent.guardrails import Guardrails
from vtai_agent.tools.automode import AutoModeInput, AutoModeTool
from vtai_agent.tools.delegate import DelegateCodingTool
from vtai_agent.tools.registry import ToolError


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


class _Proc:
    def __init__(self, code: int, out: bytes = b"", err: bytes = b""):
        self.returncode = code
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


def _fake_exec_factory(proc: _Proc, seen: dict):
    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        seen["cwd"] = kwargs.get("cwd")
        return proc

    return fake_exec


def test_opencode_argv_has_no_dir_flag(sandbox, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec_factory(_Proc(0, b"answer", b""), seen),
    )
    tool = AutoModeTool(_g(sandbox), model=None)
    reply = asyncio.run(
        tool._ask_cli("opencode", "opencode", "hi", sandbox.writable, 10)
    )
    assert reply == "answer"
    assert "--dir" not in seen["argv"]
    assert seen["argv"][:2] == ["opencode", "run"]
    assert seen["cwd"] == str(sandbox.writable)


def test_ask_cli_nonzero_exit_raises_despite_stdout(sandbox, monkeypatch):
    """opencode prints help on bad flags with a non-zero exit — that text
    must never come back as a successful 'reply' for the judge to approve."""
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_exec_factory(_Proc(1, b"DESCRIPTION\n  Run OpenCode", b""), {}),
    )
    tool = AutoModeTool(_g(sandbox), model=None)
    with pytest.raises(ToolError, match="exited 1"):
        asyncio.run(
            tool._ask_cli("opencode", "opencode", "hi", sandbox.writable, 10)
        )


def test_delegate_opencode_argv_has_no_dir_flag(sandbox):
    tool = DelegateCodingTool(_g(sandbox))
    argv = tool._build_argv(
        "opencode",
        tool.InputModel(
            cli="opencode", prompt="do it", working_dir=str(sandbox.writable)
        ),
    )
    assert "--dir" not in argv
    assert argv[:2] == ["opencode", "run"]


class _DeadJudge:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        raise RuntimeError("connection refused")


def test_judge_unavailable_fails_closed(sandbox, monkeypatch):
    """A dead judge must abort with ok=False, never report success."""
    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return "a plausible but unverified answer"

    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    # run() checks shutil.which before _ask_cli; CI runners have no opencode.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/opencode")
    # _judge does `from pydantic_ai import Agent` at call time, so patch it
    # at the source module, not on automode.
    monkeypatch.setattr("pydantic_ai.Agent", _DeadJudge)
    tool = AutoModeTool(_g(sandbox))
    r = asyncio.run(
        tool.run(AutoModeInput(
            goal="x", working_dir=str(sandbox.writable), dry_run=False))
    )
    assert not r.ok
    assert "judge unavailable" in r.summary


class _FlakyJudge:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("transient inference failure")
        return _OkResult()


class _OkResult:
    output = "OK"


def test_judge_retries_transient_failure(sandbox, monkeypatch):
    """One flaky judge call must not abort the loop; retry then verify."""
    _FlakyJudge.calls = 0
    monkeypatch.setattr("pydantic_ai.Agent", _FlakyJudge)
    tool = AutoModeTool(_g(sandbox), model=object())
    verdict, _ = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "ok"
    assert _FlakyJudge.calls == 2


def test_judge_build_failure_fails_closed(sandbox, monkeypatch):
    """A misconfigured provider (build_model raises) must return the error
    verdict, not crash out of _judge."""
    import vtai_agent.providers as providers

    monkeypatch.setattr(providers, "build_model", lambda settings: (_ for _ in ()).throw(
        RuntimeError("no provider configured")))
    tool = AutoModeTool(_g(sandbox), model=None)
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "error"
    assert "judge unavailable" in critique
