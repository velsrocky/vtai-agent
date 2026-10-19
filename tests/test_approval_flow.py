"""The pending-approval workflow for delegate_coding.

Non-interactive runs must keep the delegate's changes in the worktree as a
resolvable pending approval (never silently roll back), unresolved approvals
must block further delegate work in that repo (even with require_approval
flipped), and `resolve_pending` must both keep (with the index handed back)
and roll back."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from vtai_agent import db
from vtai_agent.guardrails import Guardrails
from vtai_agent.tools.delegate import DelegateCodingInput, DelegateCodingTool
from vtai_agent.tools.registry import ToolError

_REAL_WHICH = shutil.which


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "committed.txt").write_text("c")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def _fake_delegate(monkeypatch):
    async def fake_run(self, argv, cwd, timeout):
        (cwd / "edited.txt").write_text("delegate was here")
        return "did the edit", "", 0

    monkeypatch.setattr(DelegateCodingTool, "_run", fake_run)
    monkeypatch.setattr(shutil, "which",
                        lambda n: _REAL_WHICH(n) or f"/usr/bin/{n}")


_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@_needs_git
def test_non_interactive_delegate_keeps_changes_as_pending(sandbox, monkeypatch):
    repo = _git_repo(sandbox.writable / "repoP")

    _fake_delegate(monkeypatch)
    tool = DelegateCodingTool(_g(sandbox))
    r = asyncio.run(tool.run(DelegateCodingInput(
        cli="pi", prompt="edit", working_dir=str(repo), dry_run=False)))

    assert not r.ok
    assert r.detail["needs_approval"] is True
    assert "vtai approve" in r.summary
    # The changes stay in the worktree for the human to review (the old
    # behavior silently discarded them in every unattended session).
    assert (repo / "edited.txt").read_text() == "delegate was here"
    assert (repo / "committed.txt").read_text() == "c"

    pa = db.get_pending_approval(r.detail["approval_id"])
    assert pa is not None and pa.resolved is False
    assert pa.working_dir == str(repo)
    assert pa.diff.strip()  # diff captured for the audit trail
    assert pa.snapshot == ""  # repo was clean pre-run: HEAD is the restore point
    assert pa.orig_index


@_needs_git
def test_unresolved_pending_blocks_new_delegate_runs(sandbox, monkeypatch):
    repo = _git_repo(sandbox.writable / "repoB")
    db.create_pending_approval(run_id=None, cli="pi", prompt="earlier task",
                               working_dir=str(repo), snapshot="",
                               orig_index="", diff="x")

    _fake_delegate(monkeypatch)
    tool = DelegateCodingTool(_g(sandbox))
    for require_approval in (True, False):
        with pytest.raises(ToolError, match="unresolved approval"):
            asyncio.run(tool.run(DelegateCodingInput(
                cli="pi", prompt="edit", working_dir=str(repo),
                dry_run=False, require_approval=require_approval)))


@_needs_git
def test_resolve_pending_keep_restores_index_and_resolves(sandbox):
    repo = _git_repo(sandbox.writable / "repoK")
    (repo / "edited.txt").write_text("delegate was here")
    pa = db.create_pending_approval(run_id=None, cli="pi", prompt="keep me",
                                    working_dir=str(repo), snapshot="",
                                    orig_index="", diff="a.txt")

    tool = DelegateCodingTool(_g(sandbox))
    r = asyncio.run(tool.resolve_pending(pa.id, keep=True))
    assert r.ok and (repo / "edited.txt").exists()
    assert (repo / "committed.txt").read_text() == "c"

    sa = db.get_pending_approval(pa.id)
    assert sa.resolved is True and sa.kept is True
    rc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True)
    assert "edited.txt" in rc.stdout


@_needs_git
def test_resolve_pending_rollback_restores_repo(sandbox):
    repo = _git_repo(sandbox.writable / "repoR")
    (repo / "edited.txt").write_text("delegate was here")
    pa = db.create_pending_approval(run_id=None, cli="pi", prompt="reject me",
                                    working_dir=str(repo), snapshot="",
                                    orig_index="", diff="a.txt")

    tool = DelegateCodingTool(_g(sandbox))
    r = asyncio.run(tool.resolve_pending(pa.id, keep=False))
    assert r.ok
    assert not (repo / "edited.txt").exists()
    assert (repo / "committed.txt").read_text() == "c"

    sa = db.get_pending_approval(pa.id)
    assert sa.resolved is True and sa.kept is False
    rc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True)
    assert not rc.stdout.strip()  # repo is clean again


def test_resolve_unknown_or_stale_approval_fails_cleanly(sandbox):
    tool = DelegateCodingTool(_g(sandbox))
    r = asyncio.run(tool.resolve_pending(999999, keep=True))
    assert not r.ok and "not found" in r.summary
    pa = db.create_pending_approval(run_id=None, cli="pi", prompt="dup",
                                    working_dir="nowhere", snapshot="",
                                    orig_index="", diff="")
    db.set_pending_resolved(pa.id, keep=False)
    r2 = asyncio.run(tool.resolve_pending(pa.id, keep=True))
    assert not r2.ok and "already resolved" in r2.summary
