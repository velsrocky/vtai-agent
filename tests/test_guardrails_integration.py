import asyncio
import os
import shutil
from pathlib import Path

import pytest

from vtai_agent.guardrails import Guardrails, GuardrailViolation
from vtai_agent.tools.backup import BackupSyncInput, BackupSyncTool
from vtai_agent.tools.delegate import DelegateCodingInput, DelegateCodingTool
from vtai_agent.tools.shell import ShellInput, ShellTool


def _g(sandbox):
    return Guardrails(sandbox.cfg, sandbox.settings)


# ---------- run_shell tool ----------
def test_shell_dry_run_does_not_execute(sandbox):
    tool = ShellTool(_g(sandbox))
    target = sandbox.writable / "made"
    r = asyncio.run(tool.run(ShellInput(command=f"mkdir {target}")))
    assert r.dry_run and r.ok
    assert not target.exists()


def test_shell_execute_creates_inside_roots(sandbox):
    tool = ShellTool(_g(sandbox))
    target = sandbox.writable / "made"
    r = asyncio.run(tool.run(ShellInput(command=f"mkdir {target}", dry_run=False)))
    assert r.ok and target.is_dir()


def test_shell_execute_denied(sandbox):
    tool = ShellTool(_g(sandbox))
    with pytest.raises(GuardrailViolation):
        asyncio.run(tool.run(
            ShellInput(command=f"mkdir {sandbox.tmp}/nope", dry_run=False)))


def test_shell_execute_honors_kill_switch(sandbox):
    (sandbox.settings.paths.data_dir / "KILL").touch()
    tool = ShellTool(_g(sandbox))
    with pytest.raises(GuardrailViolation):
        asyncio.run(tool.run(
            ShellInput(command=f"mkdir {sandbox.writable}/x", dry_run=False)))


def test_shell_children_get_scrubbed_env(sandbox, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "should-not-leak")
    tool = ShellTool(_g(sandbox))
    r = asyncio.run(tool.run(
        ShellInput(command="cat /proc/self/environ", dry_run=False)))
    assert b"FAKE_API_KEY" not in r.detail["stdout"].encode(errors="replace")


# ---------- backup ----------
def test_backup_dry_run_creates_nothing(sandbox):
    src = sandbox.writable / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    dst = sandbox.writable / "bak"
    tool = BackupSyncTool(_g(sandbox))
    r = asyncio.run(tool.run(BackupSyncInput(source=str(src), destination=str(dst))))
    assert r.dry_run
    assert not dst.exists()


def test_backup_destination_outside_roots_denied(sandbox):
    tool = BackupSyncTool(_g(sandbox))
    with pytest.raises(GuardrailViolation):
        asyncio.run(tool.run(BackupSyncInput(
            source=str(sandbox.writable), destination=str(sandbox.tmp / "elsewhere"))))


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_backup_execute_transfers(sandbox):
    src = sandbox.writable / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    dst = sandbox.writable / "bak"
    tool = BackupSyncTool(_g(sandbox))
    r = asyncio.run(tool.run(BackupSyncInput(
        source=str(src), destination=str(dst), dry_run=False, verify=True)))
    assert r.ok and (dst / "a.txt").read_text() == "a"


# ---------- delegate ----------
def test_delegate_requires_git_repo(sandbox):
    tool = DelegateCodingTool(_g(sandbox))
    plain = sandbox.writable / "plain"
    plain.mkdir()
    with pytest.raises(NotADirectoryError):
        asyncio.run(tool.run(DelegateCodingInput(
            cli="pi", prompt="noop", working_dir=str(plain))))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("cli", ["opencode", "claude", "pi"])
def test_delegate_dry_run_touches_nothing(sandbox, cli):
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not installed")
    repo = sandbox.writable / "repo"
    repo.mkdir()
    init = (f"git -C {repo} init -q && git -C {repo} config user.email t@t && "
            f"git -C {repo} config user.name t")
    assert os.system(init) == 0
    (repo / "committed.txt").write_text("c")
    assert os.system(f"git -C {repo} add -A && git -C {repo} commit -qm init") == 0
    (repo / "untracked.txt").write_text("u")

    tool = DelegateCodingTool(_g(sandbox))
    r = asyncio.run(tool.run(DelegateCodingInput(
        cli=cli, prompt="noop", working_dir=str(repo))))
    assert r.dry_run
    # the dry run must not stage/snapshot: untracked file still shows as ??
    status = os.popen(f"git -C {repo} status --porcelain").read()
    assert "?? untracked.txt" in status
    assert not Path(repo, ".git/refs/stash").exists()
