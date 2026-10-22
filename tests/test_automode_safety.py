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
    reply, cost = asyncio.run(
        tool._ask_cli("opencode", "opencode", "hi", sandbox.writable, 10)
    )
    assert reply == "answer"
    assert cost is None  # opencode headless output carries no spend report
    assert "--dir" not in seen["argv"]
    # argv[0] is the resolved executable (may be an absolute path or a
    # cmd.exe /c shim wrap on Windows), never a bare unresolvable name.
    assert "opencode" in seen["argv"][0] or seen["argv"][0] == "cmd.exe"
    assert "run" in seen["argv"]
    # detached from the shared background service: the loop must work in
    # its own cwd, not whatever interactive instance owns the service
    assert "--standalone" in seen["argv"]
    assert seen["argv"][-1] == "hi"
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
    assert argv[:3] == ["opencode", "run", "--standalone"]


class _DeadJudge:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        raise RuntimeError("connection refused")


def test_judge_unavailable_fails_closed(sandbox, monkeypatch):
    """A dead judge must abort with ok=False, never report success."""
    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return "a plausible but unverified answer", None

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


# ---------- strict verdict token ----------
class _JudgeResult:
    def __init__(self, text: str, usage=None):
        self.output = text
        self.usage = usage


class _StaticJudge:
    def __init__(self, result: _JudgeResult, **kwargs):
        self._result = result

    async def run(self, *args, **kwargs):
        return self._result


class _SequenceJudge:
    """A judge whose calls return scripted outputs in order (classic
    panel-of-nondeterministic-verdicts probe). An exception entry is
    raised instead of returned; an exhausted script yields INSUFFICIENT."""

    def __init__(self, script: list, **kwargs):
        self._script = list(script)
        self.calls = 0

    async def run(self, *args, **kwargs):
        self.calls += 1
        item = (self._script.pop(0) if self._script
                else "INSUFFICIENT: script exhausted")
        if isinstance(item, Exception):
            raise item
        return _JudgeResult(item)


def test_judge_accepts_only_bare_ok(sandbox, monkeypatch):
    cases = [
        ("OK", "ok", ""),
        ("OK.", "ok", ""),
        ("  OK  ", "ok", ""),
        ("OK, but check the timestamps", "insufficient",
         "OK, but check the timestamps"),
        ("Okay, task done", "insufficient", "Okay, task done"),
        ("INSUFFICIENT: missing verification", "insufficient",
         "missing verification"),
        # Verbose approval (live-caught in the judge dogfood): first sentence
        # is exactly OK, remainder praises — accepted.
        ("OK. The reply correctly identifies the creation of hello.sh and "
         "provides the exact program output (DOGFOOD-OK).", "ok", ""),
        ("ok\nthe program printed DOGFOOD-OK as required.", "ok", ""),
        ("OK: the goal is fully satisfied.", "ok", ""),
        # Verbose rejection: the prose announces a defect — must stay
        # insufficient with the whole reply as critique (no colon).
        ("OK. However, the output line is not shown.", "insufficient",
         "OK. However, the output line is not shown."),
        ("OK. The reply is missing the absolute file path.", "insufficient",
         "OK. The reply is missing the absolute file path."),
        # Hedge class stays rejected: attached word after OK.
        ("OKay, everything checks out", "insufficient",
         "OKay, everything checks out"),
        # Known false-reject (documented, fail-closed): praise containing a
        # caveat word costs one extra loop probe instead of a wrong approval.
        ("OK. Nothing is missing from the reply.", "insufficient",
         "OK. Nothing is missing from the reply."),
        # Empty reply falls back to the fixed message.
        ("", "insufficient", "reply did not satisfy the goal"),
    ]
    for text, expected_verdict, expected_critique in cases:
        monkeypatch.setattr(
            "pydantic_ai.Agent",
            lambda *a, **k: _StaticJudge(_JudgeResult(text)))
        tool = AutoModeTool(_g(sandbox), model=object())
        verdict, critique = asyncio.run(tool._judge("goal", "reply"))
        assert verdict == expected_verdict, f"verdict for {text!r}"
        assert critique == expected_critique, f"critique for {text!r}"


# ---------- judge budget ----------
def _paid_settings(sandbox, budget: float, n: int = 1):
    from vtai_agent.config import AutoMode, Guardrails as GC, ProviderOpenAI, Settings

    return Settings(
        active_provider="openai",
        openai=ProviderOpenAI(model="test", cost_per_1k_input=1.0, cost_per_1k_output=1.0),
        guardrails=GC(
            budget_usd_per_run=budget,
            deny_globs=sandbox.cfg.deny_globs,
            trusted_bin_dirs=sandbox.cfg.trusted_bin_dirs,
        ),
        paths=sandbox.paths,
        auto_mode=AutoMode(judge_replicas=n),
    )


def _replica_settings(sandbox, n: int):
    """Local-provider Settings with a judge panel of n replicas (its spend
    est is None locally, so replica calls stay budget-free in these tests)."""
    from vtai_agent.config import AutoMode, Guardrails as GC, Settings

    return Settings(
        active_provider="local",
        guardrails=GC(
            budget_usd_per_run=10.0,
            deny_globs=sandbox.cfg.deny_globs,
            trusted_bin_dirs=sandbox.cfg.trusted_bin_dirs,
        ),
        paths=sandbox.paths,
        auto_mode=AutoMode(judge_replicas=n),
    )


def test_judge_budget_exceeded_fails_closed(sandbox, monkeypatch):
    from types import SimpleNamespace

    paid = _paid_settings(sandbox, 0.001)
    usage = SimpleNamespace(input_tokens=5000, output_tokens=0, cost=None)
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: _StaticJudge(_JudgeResult("OK", usage)))
    tool = AutoModeTool(_g(sandbox), settings=paid, model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "error"
    assert "judge budget exceeded" in critique


def test_judge_honors_budget_remaining_param(sandbox, monkeypatch):
    """The loop passes budget_remaining = config budget - CLI spend. A dead
    assignment used to overwrite that parameter with the flat config value,
    so a multi-iteration run never tightened the judge's real budget."""
    from types import SimpleNamespace

    paid = _paid_settings(sandbox, 10.0)
    usage = SimpleNamespace(input_tokens=5000, output_tokens=0, cost=None)
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: _StaticJudge(_JudgeResult("OK", usage)))
    tool = AutoModeTool(_g(sandbox), settings=paid, model=object())
    # $5.00 of judge spend fits the $10 config budget but must exceed the
    # $0.001 remaining passed by the loop.
    verdict, critique = asyncio.run(
        tool._judge("goal", "reply", budget_remaining=0.001))
    assert verdict == "error"
    assert "judge budget exceeded" in critique


def test_judge_unknown_paid_spend_fails_closed(sandbox, monkeypatch):
    paid = _paid_settings(sandbox, 10.0)
    monkeypatch.setattr(
        "pydantic_ai.Agent", lambda *a, **k: _StaticJudge(_JudgeResult("OK", usage=None)))
    tool = AutoModeTool(_g(sandbox), settings=paid, model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "error"
    assert "cannot verify judge spend" in critique


# ---------- judge replica panel ----------
def test_judge_panel_majority_ok(sandbox, monkeypatch):
    """2 of 3 replicas approving (one with verbose prose) is a majority."""
    seq = _SequenceJudge(["OK", "OK",
                          "OK. The reply correctly affirms the program output."])
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: seq)
    tool = AutoModeTool(_g(sandbox), settings=_replica_settings(sandbox, 3),
                        model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "ok"
    assert critique == ""
    assert seq.calls == 3


def test_judge_panel_minority_ok_stays_insufficient(sandbox, monkeypatch):
    """1 ok + 2 objections: no ok majority — fail closed with the joined
    critiques so the follow-up prompt carries every objection."""
    seq = _SequenceJudge(["OK",
                          "INSUFFICIENT: missing the absolute path",
                          "INSUFFICIENT: output not verified"])
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: seq)
    tool = AutoModeTool(_g(sandbox), settings=_replica_settings(sandbox, 3),
                        model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "insufficient"
    assert critique == "missing the absolute path | output not verified"
    assert seq.calls == 3


def test_judge_panel_error_majority_fails_closed(sandbox, monkeypatch):
    """All replicas flaking (each retries twice) aggregates to 'error' —
    the loop then stops instead of looping blindly."""
    boom = RuntimeError("connection refused")
    seq = _SequenceJudge([boom, boom] * 3)
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: seq)
    tool = AutoModeTool(_g(sandbox), settings=_replica_settings(sandbox, 3),
                        model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "error"
    assert "judge unavailable" in critique
    assert seq.calls == 6  # _JUDGE_ATTEMPTS retries per replica


def test_judge_panel_even_tie_stays_insufficient(sandbox, monkeypatch):
    """n=2 and a 1-1 split: no strict majority either way — insufficient."""
    seq = _SequenceJudge(["OK", "INSUFFICIENT: unverified claim"])
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: seq)
    tool = AutoModeTool(_g(sandbox), settings=_replica_settings(sandbox, 2),
                        model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "insufficient"
    assert "unverified claim" in critique
    assert seq.calls == 2


def test_judge_panel_stops_spending_on_budget(sandbox, monkeypatch):
    """Shared budget across replicas: the $5-per-call cost blew the $8
    budget on the second replica, so the panel must stop — the third
    replica (and its spend) never happens."""
    from types import SimpleNamespace

    paid = _paid_settings(sandbox, 8.0, n=3)
    usage = SimpleNamespace(input_tokens=5000, output_tokens=0, cost=None)
    seq = _SequenceJudge(["OK", "OK", "OK"])
    orig_run = seq.run

    async def run_with_usage(*args, **kwargs):
        result = await orig_run(*args, **kwargs)
        result.usage = usage  # mimic runnable meters on paid providers
        return result

    seq.run = run_with_usage
    monkeypatch.setattr("pydantic_ai.Agent", lambda *a, **k: seq)
    tool = AutoModeTool(_g(sandbox), settings=paid, model=object())
    verdict, critique = asyncio.run(tool._judge("goal", "reply"))
    assert verdict == "error"
    assert "judge budget exceeded" in critique
    assert "$10.0000" in critique  # replica 1 ($5.00) + replica 2 ($5.00 — the budget-killer)
    assert seq.calls == 2  # replica 3 never incurred spend


# ---------- deputy CLI spend metering ----------
def test_resolve_cli_argv_wraps_windows_cmd_shim(monkeypatch):
    import os

    from vtai_agent.tools.registry import resolve_cli_argv

    monkeypatch.setattr(shutil, "which", lambda n: r"C:\npm\opencode.cmd")
    argv = resolve_cli_argv("opencode")
    if os.name == "nt":
        assert argv == ["cmd.exe", "/c", r"C:\npm\opencode.cmd"]
    else:
        assert argv == [r"C:\npm\opencode.cmd"]

    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/opencode")
    assert resolve_cli_argv("opencode") == ["/usr/bin/opencode"]

    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert resolve_cli_argv("opencode") is None


def test_claude_argv_uses_json_output(sandbox, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_exec_factory(_Proc(0, b'{"result": "ok answer", "total_cost_usd": 0.02}', b""),
                           seen),
    )
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")
    tool = AutoModeTool(_g(sandbox), model=None)
    reply, cost = asyncio.run(
        tool._ask_cli("claude", "claude", "fix", sandbox.writable, 10))
    assert reply == "ok answer"
    assert cost == pytest.approx(0.02)
    assert "--output-format" in seen["argv"]
    assert "json" in seen["argv"]


def test_claude_argv_missing_cost_reports_none(sandbox, monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_exec_factory(_Proc(0, b'{"result": "did it"}', b""), {}),
    )
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")
    tool = AutoModeTool(_g(sandbox), model=None)
    reply, cost = asyncio.run(
        tool._ask_cli("claude", "claude", "fix", sandbox.writable, 10))
    assert reply == "did it"
    assert cost is None  # run() will fail closed for a cost-capable CLI


def _auto_ok_judge(sandbox, monkeypatch):
    monkeypatch.setattr(
        "pydantic_ai.Agent", lambda *a, **k: _StaticJudge(_JudgeResult("OK")))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")


def test_auto_mode_aborts_when_claude_spend_unverifiable(sandbox, monkeypatch):
    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return "looks done", None

    _auto_ok_judge(sandbox, monkeypatch)
    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    tool = AutoModeTool(_g(sandbox), model=object())
    r = asyncio.run(tool.run(AutoModeInput(
        goal="x", working_dir=str(sandbox.writable), cli="claude", dry_run=False)))
    assert not r.ok
    assert "cannot verify claude spend" in r.summary
    assert r.detail["spend"]["cli_usd"] is None


def test_auto_mode_meters_claude_spend_against_budget(sandbox, monkeypatch):
    from vtai_agent.config import Guardrails as GC, Settings

    paid = Settings(
        guardrails=GC(budget_usd_per_run=0.10,
                      deny_globs=sandbox.cfg.deny_globs,
                      trusted_bin_dirs=sandbox.cfg.trusted_bin_dirs),
        paths=sandbox.paths,
    )
    calls = {"n": 0}
    replies = ["first", "second"]

    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        calls["n"] += 1
        return replies[min(calls["n"] - 1, 1)], 0.34

    _auto_ok_judge(sandbox, monkeypatch)
    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    tool = AutoModeTool(_g(sandbox), settings=paid, model=object())
    r = asyncio.run(tool.run(AutoModeInput(
        goal="x", working_dir=str(sandbox.writable), cli="claude",
        max_iterations=3, dry_run=False)))
    assert not r.ok
    assert "budget" in r.summary and "0.34" in r.summary
    assert calls["n"] == 1  # aborted before a second paid dispatch
    assert r.detail["spend"]["cli_usd"] == pytest.approx(0.34)


def test_auto_mode_meters_and_succeeds_within_budget(sandbox, monkeypatch):
    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        if "REVIEWER'S CRITIQUE" in prompt:
            return "second try", 0.05
        return "first try", 0.20

    class _TwoPartJudge:
        calls = 0

        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            type(self).calls += 1
            text = "OK" if type(self).calls > 1 else "INSUFFICIENT: incomplete"
            return _JudgeResult(text)

    _TwoPartJudge.calls = 0
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    monkeypatch.setattr("pydantic_ai.Agent", _TwoPartJudge)
    tool = AutoModeTool(_g(sandbox), model=object())
    r = asyncio.run(tool.run(AutoModeInput(
        goal="fix the test", working_dir=str(sandbox.writable), cli="claude",
        max_iterations=2, dry_run=False)))
    assert r.ok and r.detail["iterations"] == 2
    assert r.detail["spend"]["cli_usd"] == pytest.approx(0.25)
    assert [h["cli_cost_usd"] for h in r.detail["history"]] == [0.20, 0.05]


def test_auto_mode_opencode_spend_labeled_unmeasured(sandbox, monkeypatch):
    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return "fine", None

    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/opencode")
    monkeypatch.setattr(
        "pydantic_ai.Agent", lambda *a, **k: _StaticJudge(_JudgeResult("OK")))
    tool = AutoModeTool(_g(sandbox), model=object())
    r = asyncio.run(tool.run(AutoModeInput(
        goal="x", working_dir=str(sandbox.writable), cli="opencode",
        dry_run=False)))
    assert r.ok
    assert r.detail["spend"]["cli_usd"] == "unmeasured"
