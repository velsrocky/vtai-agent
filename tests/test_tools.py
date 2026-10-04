"""Direct tests for the smaller tools: organize_files, delegate rollback,
media job enumeration. Guardrails are covered in test_guardrails*."""

import asyncio
import shutil

import subprocess
from pathlib import Path

import pytest

from vtai_agent.guardrails import Guardrails
from vtai_agent.tools.delegate import DelegateCodingInput, DelegateCodingTool
from vtai_agent.tools.files import OrganizeFilesInput, OrganizeFilesTool
from vtai_agent.tools.media import MediaTranscodeTool, TranscodeMediaInput

_REAL_WHICH = shutil.which


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


# ---------- organize_files ----------
def test_organize_dry_run_plans_without_moving(sandbox, tmp_path):
    d = sandbox.writable / "messy"
    d.mkdir()
    (d / "a.pdf").write_text("x")
    (d / "b.jpg").write_text("x")
    tool = OrganizeFilesTool(_g(sandbox))
    r = asyncio.run(tool.run(OrganizeFilesInput(
        directory=str(d), dry_run=True, confirm_mime=False)))
    assert r.ok and r.dry_run
    assert (d / "a.pdf").exists() and (d / "b.jpg").exists()
    assert {p["category"] for p in r.detail["plan"]} == {"Documents", "Images"}


def test_organize_execute_sorts_and_renames_collisions(sandbox):
    d = sandbox.writable / "messy2"
    d.mkdir()
    (d / "a.pdf").write_text("one")
    (d / "Documents").mkdir()
    (d / "Documents" / "a.pdf").write_text("two")
    tool = OrganizeFilesTool(_g(sandbox))
    r = asyncio.run(tool.run(OrganizeFilesInput(
        directory=str(d), dry_run=False, confirm_mime=False)))
    assert r.ok and r.detail["moved"] >= 1
    docs = d / "Documents"
    assert (docs / "a.pdf").exists()
    assert (docs / "a_1.pdf").exists()  # collision auto-renamed


# ---------- delegate rollback ----------
def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "committed.txt").write_text("c")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_delegate_rolls_back_on_failed_verify(sandbox, monkeypatch):
    repo = _git_repo(sandbox.writable / "repo")

    async def fake_run(self, argv, cwd, timeout):
        (cwd / "edited.txt").write_text("delegate was here")
        return "did the edit", "", 0

    monkeypatch.setattr(DelegateCodingTool, "_run", fake_run)
    monkeypatch.setattr("vtai_agent.tools.delegate.shutil.which",
                        lambda n: _REAL_WHICH(n) or f"/usr/bin/{n}")
    tool = DelegateCodingTool(_g(sandbox))
    # `ls /nonexistent-definitely` exits non-zero -> verify gate fails
    r = asyncio.run(tool.run(DelegateCodingInput(
        cli="pi", prompt="edit", working_dir=str(repo), dry_run=False,
        verify_command="ls /nonexistent-definitely", require_approval=False,
    )))
    assert not r.ok
    assert r.detail.get("rolled_back") is True
    assert not (repo / "edited.txt").exists()
    assert (repo / "committed.txt").read_text() == "c"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_delegate_keeps_changes_when_verify_passes(sandbox, monkeypatch):
    repo = _git_repo(sandbox.writable / "repo2")

    async def fake_run(self, argv, cwd, timeout):
        (cwd / "edited.txt").write_text("delegate was here")
        return "did the edit", "", 0

    monkeypatch.setattr(DelegateCodingTool, "_run", fake_run)
    monkeypatch.setattr("vtai_agent.tools.delegate.shutil.which",
                        lambda n: _REAL_WHICH(n) or f"/usr/bin/{n}")
    tool = DelegateCodingTool(_g(sandbox))
    # `ls <repo>` succeeds -> verify gate passes -> changes kept
    r = asyncio.run(tool.run(DelegateCodingInput(
        cli="pi", prompt="edit", working_dir=str(repo), dry_run=False,
        verify_command=f"ls {sandbox.writable}", require_approval=False,
    )))
    assert r.ok
    assert (repo / "edited.txt").read_text() == "delegate was here"


# ---------- media job enumeration ----------
def test_media_dry_run_skips_output_dir(sandbox):
    d = sandbox.writable / "vids"
    (d / "transcoded").mkdir(parents=True)
    (d / "a.mp4").write_bytes(b"x")
    (d / "transcoded" / "b.mp4").write_bytes(b"x")
    tool = MediaTranscodeTool(_g(sandbox))
    r = asyncio.run(tool.run(TranscodeMediaInput(
        directory=str(d), dry_run=True)))
    assert r.ok and r.dry_run
    sources = [j["source"] for j in r.detail["jobs"]]
    assert sources == [str(d / "a.mp4")]


# ---------- auto_mode ----------
def _judge_model(verdicts: list[str]):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        v = verdicts[min(calls["n"] - 1, len(verdicts) - 1)]
        return ModelResponse(parts=[TextPart(v)])

    return FunctionModel(fn)


def test_auto_mode_stops_when_judge_approves(sandbox, monkeypatch, tmp_path):
    from vtai_agent.tools.automode import AutoModeInput, AutoModeTool

    replies = iter(["wrong answer", "correct answer"])

    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return next(replies)

    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    monkeypatch.setattr(
        "vtai_agent.tools.automode.shutil.which", lambda n: f"/usr/bin/{n}")
    tool = AutoModeTool(_g(sandbox), model=_judge_model(
        ["INSUFFICIENT: off by one", "OK"]))
    r = asyncio.run(tool.run(AutoModeInput(
        goal="add a and b", working_dir=str(sandbox.writable), dry_run=False)))
    assert r.ok and r.detail["iterations"] == 2
    assert r.detail["final_answer"] == "correct answer"
    assert r.detail["history"][0]["critique"].startswith("off by one")


def test_auto_mode_gives_up_after_max_iterations(sandbox, monkeypatch):
    from vtai_agent.tools.automode import AutoModeInput, AutoModeTool

    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        return "still wrong"

    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    monkeypatch.setattr(
        "vtai_agent.tools.automode.shutil.which", lambda n: f"/usr/bin/{n}")
    tool = AutoModeTool(_g(sandbox), model=_judge_model(["INSUFFICIENT: nope"]))
    r = asyncio.run(tool.run(AutoModeInput(
        goal="x", working_dir=str(sandbox.writable), max_iterations=2,
        dry_run=False)))
    assert not r.ok and r.detail["iterations"] == 2


def test_auto_mode_dry_run_does_not_run(sandbox, monkeypatch):
    from vtai_agent.tools.automode import AutoModeInput, AutoModeTool

    called = {"n": 0}

    async def fake_cli(self, binary, cli, prompt, cwd, timeout):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(AutoModeTool, "_ask_cli", fake_cli)
    monkeypatch.setattr(
        "vtai_agent.tools.automode.shutil.which", lambda n: f"/usr/bin/{n}")
    tool = AutoModeTool(_g(sandbox))
    r = asyncio.run(tool.run(AutoModeInput(
        goal="x", working_dir=str(sandbox.writable), dry_run=True)))
    assert r.ok and r.dry_run and called["n"] == 0
