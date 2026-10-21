"""inspect_application (REA wrapper) tests: capability gating, quarantine
copy fidelity, deny-skip deep parity, judge plumbing. Guardrail and
orchestrator policy live in test_guardrails* / test_automode_*."""

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from vtai_agent.config import Capabilities, CapabilityRea
from vtai_agent.guardrails import GuardrailViolation, Guardrails
from vtai_agent.tools.rea import InspectApplicationInput, InspectApplicationTool

FAKE_REA_BODY = """#!/usr/bin/env bash
printf '%s\\n' "$@" > {argvlog}
cat <<'JSON'
{{"evidence_id": "{evid}", "normalized_result": {{"modules": ["app/main.js"]}}}}
JSON
"""

# Windows sibling of the same fake: a .cmd batch shim (npm installs REA as
# rea.cmd there). Redirect-first form avoids cmd's digit-swallowing redirect
# parse; one arg per line matches the POSIX fake's argvlog shape.
FAKE_REA_BODY_NT = """@echo off
>"{argvlog}" echo %~1
>>"{argvlog}" echo %~2
>>"{argvlog}" echo %~3
echo {{"evidence_id": "{evid}", "normalized_result": {{"modules": ["app/main.js"]}}}}
"""


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


@pytest.fixture
def rea_enabled(sandbox):
    sandbox.settings.capabilities = Capabilities(
        rea=CapabilityRea(enabled=True, binary="rea",
                          command_prefix=["analyze-javascript-application"]))
    return sandbox


@pytest.fixture
def fake_rea(rea_enabled, tmp_path, monkeypatch):
    """A shelved fake `rea` binary that records its argv and emits JSON."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    argvlog = tmp_path / "rea-argv.log"
    body = (FAKE_REA_BODY_NT if os.name == "nt" else FAKE_REA_BODY)
    script = fakebin / ("rea.cmd" if os.name == "nt" else "rea")
    script.write_text(body.format(argvlog=argvlog, evid="ev-fake-1"))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH",
                       str(fakebin) + os.pathsep + os.environ.get("PATH", ""))
    return SimpleNamespace(bin=fakebin, argvlog=argvlog)


def _app_tree(where: Path) -> Path:
    app = where / "SomeApp"
    app.mkdir()
    (app / "package.json").write_text('{"name":"someapp"}')
    (app / "main.js").write_text("export const csv = () => []")
    return app


def _tool(sandbox) -> InspectApplicationTool:
    return InspectApplicationTool(_g(sandbox), sandbox.settings)


# ---------- gating ----------
def test_disabled_capability_fails_closed(sandbox):
    with pytest.raises(Exception, match="disabled in config"):
        asyncio.run(_tool(sandbox).run(InspectApplicationInput(
            goal="x", target=".", judge=False)))


def test_command_prefix_rejects_paths_and_metachars(sandbox):
    sandbox.settings.capabilities = Capabilities(
        rea=CapabilityRea(enabled=True, command_prefix=["analyze /etc/x"]))
    t = _tool(sandbox)
    (sandbox.tmp / "appdir").mkdir()
    with pytest.raises(Exception, match="command token rejected"):
        asyncio.run(t.run(InspectApplicationInput(
            goal="x", target=str(sandbox.tmp / "appdir"), dry_run=True)))


def test_deny_globs_apply_to_target(rea_enabled, fake_rea):
    t = _tool(rea_enabled)
    with pytest.raises(GuardrailViolation, match="Protected path"):
        asyncio.run(t.run(InspectApplicationInput(
            goal="x", target=str(rea_enabled.protected / "secret.txt"))))


def test_missing_target_is_a_tool_error(sandbox, fake_rea):
    with pytest.raises(Exception, match="does not exist"):
        asyncio.run(_tool(sandbox).run(InspectApplicationInput(
            goal="x", target=str(sandbox.tmp / "nope"), judge=False)))


def test_missing_binary_fails_with_official_source_hint(rea_enabled, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(Exception, match="official"):
        asyncio.run(_tool(rea_enabled).run(InspectApplicationInput(
            goal="x", target=".", judge=False)))


# ---------- plan mode ----------
def test_dry_run_plans_and_runs_nothing(sandbox, fake_rea):
    app = _app_tree(sandbox.writable)
    r = asyncio.run(_tool(sandbox).run(InspectApplicationInput(
        goal="how does csv work", target=str(app), dry_run=True)))
    assert r.ok and r.dry_run
    assert r.detail["argv_prefix"] == ["analyze-javascript-application"]
    assert fake_rea.argvlog.exists() is False          # binary never ran
    qroot = sandbox.tmp / "data" / "quarantine"
    if qroot.exists():
        assert not list(qroot.rglob("SomeApp"))


# ---------- quarantine fidelity ----------
def test_exec_runs_on_copy_only(sandbox, fake_rea):
    app = _app_tree(sandbox.writable)
    r = asyncio.run(_tool(sandbox).run(InspectApplicationInput(
        goal="explain csv export", target=str(app), judge=False,
        dry_run=False)))
    assert r.ok
    argv = fake_rea.argvlog.read_text().splitlines()
    assert argv[0] == "analyze-javascript-application"
    copy_arg = argv[1]
    assert copy_arg.startswith(str(sandbox.tmp / "data" / "quarantine"))
    assert argv[-1] == "--json"
    assert argv.index(copy_arg) == 1                   # no extras between target and --json
    assert Path(copy_arg).exists() is False            # quarantine cleaned up
    assert r.detail["evidence_id"] == "ev-fake-1"
    assert r.detail["evidence_digest"].startswith("sha256:")
    assert r.detail["copy"]["files"] == 2


def test_keep_copy_preserves_quarantine(sandbox, fake_rea):
    app = _app_tree(sandbox.writable)
    r = asyncio.run(_tool(sandbox).run(InspectApplicationInput(
        goal="g", target=str(app), judge=False, keep_copy=True, dry_run=False)))
    assert r.ok and r.detail["quarantine_kept"] is True
    argv = fake_rea.argvlog.read_text().splitlines()
    copy_path = Path(argv[1])
    assert copy_path.exists() and (copy_path / "main.js").exists()


def test_denied_entries_never_reach_the_copy(sandbox, fake_rea):
    app = _app_tree(sandbox.writable)
    try:
        (app / "broken.lnk").symlink_to(sandbox.protected / "missing")
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")
    sandbox.cfg.deny_globs.append("**/.env")
    (app / ".env").write_text("SECRET=1")
    r = asyncio.run(_tool(sandbox).run(InspectApplicationInput(
        goal="g", target=str(app), judge=False, keep_copy=True, dry_run=False)))
    assert r.ok
    copy_path = Path(fake_rea.argvlog.read_text().splitlines()[1])
    assert not (copy_path / ".env").exists()             # deny parity in depth
    assert not (copy_path / "broken.lnk").exists()
    assert r.detail["copy"]["skipped_denied"] == 1
    assert r.detail["copy"]["skipped_broken"] == 1


def test_symlink_into_protected_never_copies(sandbox, fake_rea):
    app = _app_tree(sandbox.writable)
    try:
        (app / "ssh_link").symlink_to(sandbox.protected)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")
    r = asyncio.run(_tool(sandbox).run(InspectApplicationInput(
        goal="g", target=str(app), judge=False, keep_copy=True, dry_run=False)))
    copy_path = Path(fake_rea.argvlog.read_text().splitlines()[1])
    assert not (copy_path / "ssh_link").exists()
    assert r.detail["copy"]["skipped_broken"] == 1
    assert "SECRET" not in "\n".join(                     # belt: no content leak
        p.read_text() for p in copy_path.rglob("*") if p.is_file())


# ---------- judging ----------
def test_judge_ok_and_insufficient_plumb_through(sandbox, fake_rea, monkeypatch):
    app = _app_tree(sandbox.writable)

    async def ok_judge(goal, reply):
        return "ok", ""

    tool = _tool(sandbox)
    monkeypatch.setattr(tool, "_judge", ok_judge)
    r = asyncio.run(tool.run(InspectApplicationInput(
        goal="g", target=str(app), judge=True, dry_run=False)))
    assert r.ok and r.detail["judge"]["verdict"] == "ok"

    async def bad_judge(goal, reply):
        return "insufficient", "evidence has no csv trace"

    tool2 = _tool(sandbox)
    monkeypatch.setattr(tool2, "_judge", bad_judge)
    r2 = asyncio.run(tool2.run(InspectApplicationInput(
        goal="g", target=str(app), judge=True, dry_run=False)))
    assert not r2.ok and "csv trace" in r2.summary


def test_non_json_output_survives(rea_enabled, tmp_path, monkeypatch):
    fakebin = tmp_path / "fakebin2"
    fakebin.mkdir()
    (fakebin / "rea").write_text("#!/usr/bin/env bash\necho 'not json'\n")
    (fakebin / "rea").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ.get('PATH', '')}")
    app = _app_tree(rea_enabled.writable)
    r = asyncio.run(_tool(rea_enabled).run(InspectApplicationInput(
        goal="g", target=str(app), judge=False, dry_run=False)))
    assert r.ok
    raw = r.detail["normalized_result"]["raw"]
    assert raw.strip() == "not json"
