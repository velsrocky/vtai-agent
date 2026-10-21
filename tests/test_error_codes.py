"""Error taxonomy: every fail-closed surface carries a stable code from
errors.CODES, appended as "[code=E_...]" and attached as an exception
attribute. Judges react to codes; prose stays authoritative."""

import asyncio
import os
import stat

import pytest

from vtai_agent.errors import CODES, explain, suffix
from vtai_agent.guardrails import GuardrailViolation, Guardrails
from vtai_agent.tools.rea import InspectApplicationInput, InspectApplicationTool


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


# ---------- catalog sanity ----------
def test_codes_are_unique_and_namespaced():
    assert len(set(CODES)) == len(CODES)
    for code in CODES:
        assert code.startswith("E_")
        assert "\n" not in CODES[code]


def test_suffix_and_explain_helpers():
    assert suffix(None) == ""
    assert suffix("E_KILL_SWITCH") == " [code=E_KILL_SWITCH]"
    assert explain("E_PATH_PROTECTED") == CODES["E_PATH_PROTECTED"]
    assert "unknown" in explain("E_NOT_A_REAL_CODE")


# ---------- guardrails: paths ----------
def test_writable_violation_carries_code(sandbox, tmp_path):
    with pytest.raises(GuardrailViolation) as ei:
        _g(sandbox).check_writable(tmp_path / "outside")
    assert ei.value.code == "E_PATH_WRITABLE"
    assert "[code=E_PATH_WRITABLE]" in str(ei.value)


def test_protected_violation_carries_code(sandbox):
    with pytest.raises(GuardrailViolation) as ei:
        _g(sandbox).check_readable(sandbox.protected / "secret.txt")
    assert ei.value.code == "E_PATH_PROTECTED"


# ---------- guardrails: shell rejection codes ----------
def _decide(sandbox, command):
    return _g(sandbox).check_shell(command, base_dir=sandbox.tmp)


def test_not_allowlisted_code(sandbox):
    d = _decide(sandbox, "curl https://example.com")
    assert d.allowed is False and "[code=E_SHELL_NOT_ALLOWLISTED]" in d.reason


def test_no_policy_code(sandbox):
    sandbox.cfg.shell_allowlist.append("unknownbin")
    d = _decide(sandbox, "unknownbin")
    assert d.allowed is False and "[code=E_SHELL_NO_POLICY]" in d.reason


def test_bin_missing_code(sandbox):
    sandbox.cfg.shell_allowlist.append("totallynotarealbin")
    sandbox.cfg.shell_extra_modes["totallynotarealbin"] = "none"
    d = _decide(sandbox, "totallynotarealbin")
    assert "[code=E_SHELL_BIN_MISSING]" in d.reason


def test_bin_untrusted_code(sandbox, tmp_path, monkeypatch):
    fakebin = tmp_path / "shadowbin"
    fakebin.mkdir()
    script = fakebin / "ls"
    script.write_text("#!/bin/sh\ntrue\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ.get('PATH', '')}")
    d = _g(sandbox).check_shell("ls", base_dir=None)
    assert d.allowed is False and "[code=E_SHELL_BIN_UNTRUSTED]" in d.reason


def test_deny_flag_code(sandbox):
    d = _decide(sandbox, f"tar --checkpoint-action=exec=bsh {sandbox.writable}")
    assert "[code=E_SHELL_FLAG_DENY]" in d.reason


def test_arg_rejected_code(sandbox):
    d = _decide(sandbox, f"grep x {sandbox.protected}/secret.txt")
    assert d.allowed is False and "[code=E_SHELL_ARG_REJECTED]" in d.reason


def test_empty_and_unparseable_codes(sandbox):
    assert "[code=E_SHELL_EMPTY]" in _decide(sandbox, '   ').reason
    assert "[code=E_SHELL_UNPARSEABLE]" in _decide(sandbox, "ls 'unterminated").reason


# ---------- global controls ----------
def test_kill_switch_code(sandbox):
    (sandbox.settings.paths.data_dir / "KILL").write_text("")
    with pytest.raises(GuardrailViolation) as ei:
        _g(sandbox).ensure_not_killed()
    assert ei.value.code == "E_KILL_SWITCH"


# ---------- capabilities ----------
def test_rea_disabled_carries_code(sandbox):
    tool = InspectApplicationTool(_g(sandbox), sandbox.settings)
    with pytest.raises(Exception) as ei:
        import asyncio
        asyncio.run(tool.run(InspectApplicationInput(goal="g", target=".")))
    assert getattr(ei.value, "code", None) == "E_CAP_DISABLED"


def test_rea_missing_binary_carries_code(sandbox, monkeypatch):
    sandbox.settings.capabilities.rea.enabled = True
    monkeypatch.setenv("PATH", "")
    tool = InspectApplicationTool(_g(sandbox), sandbox.settings)
    with pytest.raises(Exception) as ei:
        asyncio.run(tool.run(InspectApplicationInput(goal="g", target=".")))
    assert getattr(ei.value, "code", None) == "E_CAP_BIN_MISSING"


@pytest.fixture
def rea_enabled(sandbox):
    return sandbox


def test_rea_bad_token_carries_code(sandbox):
    sandbox.settings.capabilities.rea.enabled = True
    sandbox.settings.capabilities.rea.command_prefix = ["analyze /etc/x"]
    tool = InspectApplicationTool(_g(sandbox), sandbox.settings)
    with pytest.raises(Exception) as ei:
        asyncio.run(tool.run(InspectApplicationInput(goal="g", target=".")))
    assert getattr(ei.value, "code", None) == "E_CAP_TOKEN_INVALID"


def test_errors_module_is_publishable():
    """Codes are meant for agents outside this repo: a judge that only sees
    text must still be able to look a code up canonically."""
    for code, meaning in CODES.items():
        assert explain(code) == meaning
