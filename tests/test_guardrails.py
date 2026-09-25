import os

import pytest

from vtai_agent.guardrails import Guardrails, GuardrailViolation


@pytest.fixture
def g(sandbox):
    Guardrails.reset_denial_counts()
    return Guardrails(sandbox.cfg, sandbox.settings)


# ---------- paths ----------
def test_check_writable_allowed(g, sandbox):
    f = sandbox.writable / "test.txt"
    assert g.check_writable(f) == f.resolve()


def test_check_writable_denied_by_glob(g, sandbox):
    with pytest.raises(GuardrailViolation):
        g.check_writable(sandbox.protected / "secret.txt")


def test_check_writable_outside_roots(g, tmp_path):
    with pytest.raises(GuardrailViolation):
        g.check_writable(tmp_path / "outside.txt")


def test_check_writable_blocks_dotdot_escape(g, sandbox):
    sneaky = sandbox.writable / ".." / "protected" / "secret.txt"
    with pytest.raises(GuardrailViolation):
        g.check_writable(sneaky)


def test_check_writable_blocks_symlink_escape(g, sandbox):
    outside = sandbox.settings.paths.data_dir.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    link = sandbox.writable / "link"
    link.symlink_to(outside)
    with pytest.raises(GuardrailViolation):
        g.check_writable(link / "payload.txt")


def test_check_readable_denies_protected_and_exact_dir(g):
    with pytest.raises(GuardrailViolation):
        g.check_readable("~/.ssh/id_rsa")
    with pytest.raises(GuardrailViolation):  # the dir itself, not just children
        g.check_readable("~/.ssh")


def test_is_denied_helper(g, sandbox):
    assert g.is_denied(sandbox.protected / "secret.txt")
    assert not g.is_denied(sandbox.writable / "a.txt")


# ---------- shell policy ----------
def test_shell_allowed_bare_name_resolves_trusted(g, sandbox):
    d = g.check_shell(f"ls {sandbox.writable}")
    assert d.allowed
    assert any(d.argv[0].startswith(t) for t in sandbox.cfg.trusted_bin_dirs)


def test_shell_denies_unknown_binary(g):
    d = g.check_shell("rm -rf /")
    assert not d.allowed
    assert "not in allowlist" in d.reason


def test_shell_denies_pathed_binary(g, sandbox):
    d = g.check_shell(f"/usr/bin/cat {sandbox.writable}/x")
    assert not d.allowed
    assert "bare name" in d.reason


def test_shell_denies_untrusted_resolution(g, sandbox, monkeypatch, tmp_path):
    fake = tmp_path / "evilbin"
    fake.mkdir()
    (fake / "cat").write_text("#!/bin/sh\necho pwned\n")
    (fake / "cat").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    d = g.check_shell("cat whatever")
    assert not d.allowed
    assert "trusted dirs" in d.reason


def test_shell_denies_find_exec(g):
    d = g.check_shell("find . -exec rm {} ;")
    assert not d.allowed


def test_shell_read_mode_deny_arg(g, sandbox):
    d = g.check_shell(f"cat {sandbox.protected}/secret.txt")
    assert not d.allowed
    assert "argument rejected" in d.reason


def test_shell_write_mode_requires_writable_args(g, tmp_path):
    d = g.check_shell(f"cp {tmp_path}/a.txt {tmp_path}/b.txt")
    assert not d.allowed
    assert "argument rejected" in d.reason


def test_shell_write_mode_ok_inside_roots(g, sandbox):
    src = sandbox.writable / "a.txt"
    src.write_text("x")
    d = g.check_shell(f"cp {src} {sandbox.writable / 'b.txt'}")
    assert d.allowed


def test_shell_dangerous_flags(g, sandbox):
    w = str(sandbox.writable)
    assert not g.check_shell(f"rsync -e bash {w}/a {w}/b").allowed
    assert not g.check_shell(f"rsync --rsync-path=evil {w}/a {w}/b").allowed
    assert not g.check_shell(f"tar --checkpoint-action=exec:sh cf {w}/x.tar {w}").allowed


def test_shell_extra_modes_and_base_dir(sandbox, monkeypatch):
    bin_dir = sandbox.tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mytool"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    sandbox.cfg.shell_allowlist = [*sandbox.cfg.shell_allowlist, "mytool"]
    sandbox.cfg.shell_extra_modes = {"mytool": "write"}
    sandbox.cfg.trusted_bin_dirs = [*sandbox.cfg.trusted_bin_dirs, str(bin_dir)]
    g = Guardrails(sandbox.cfg, sandbox.settings)

    # relative arg resolves against base_dir (the repo), not the agent's cwd
    d = g.check_shell("mytool tests/", base_dir=sandbox.writable)
    assert d.allowed, d.reason
    d = g.check_shell("mytool /etc/passwd")
    assert not d.allowed


def test_shell_modeless_binary_denied(g):
    d = g.check_shell("python3 something.py")
    assert not d.allowed
    assert "not in allowlist" in d.reason


# ---------- echo-loop circuit breaker ----------
def test_circuit_breaker_trips_on_repeated_denials(g):
    for _ in range(2):
        d = g.check_shell("rm -rf /loop")
        assert not d.allowed and "CIRCUIT BREAKER" not in d.reason
    d = g.check_shell("rm -rf /loop")
    assert "CIRCUIT BREAKER" in d.reason
    # a different denial is unaffected by this one's counter
    d2 = g.check_shell("rm -rf /elsewhere")
    assert "CIRCUIT BREAKER" not in d2.reason


def test_circuit_breaker_resets_after_window(g):
    for _ in range(3):
        g.check_shell("rm -rf /win")
    assert "CIRCUIT BREAKER" in g.check_shell("rm -rf /win").reason
    from vtai_agent import guardrails as GR
    GR._denial_counts[("shell", "rm -rf /win")][1] -= g.cfg.denial_window_seconds + 1
    assert "CIRCUIT BREAKER" not in g.check_shell("rm -rf /win").reason


# ---------- kill / dry-run ----------
def test_kill_switch(g, sandbox):
    assert not g.kill_switch_active()
    g.ensure_not_killed()
    (sandbox.settings.paths.data_dir / "KILL").touch()
    assert g.kill_switch_active()
    with pytest.raises(GuardrailViolation):
        g.ensure_not_killed()


def test_dry_run_default(g):
    assert g.dry_run_default
