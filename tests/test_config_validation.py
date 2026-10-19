"""validate_settings should warn (not fail) about an empty deny_globs list,
because an empty list is legal but means nothing is protected."""

import sys
from pathlib import Path

from vtai_agent.config import Guardrails, Paths, Settings, validate_settings


def _settings(tmp_path, deny_globs: list[str]) -> Settings:
    # sys.executable's dir always exists, satisfying the trusted_bin_dirs
    # "at least one real directory" validation on every CI machine.
    return Settings(
        guardrails=Guardrails(
            shell_allowlist=["ls"],
            deny_globs=deny_globs,
            trusted_bin_dirs=[str(Path(sys.executable).parent)],
        ),
        paths=Paths(writable_roots=[tmp_path], data_dir=tmp_path / "data"),
    )


def test_empty_deny_globs_warns(capsys, tmp_path):
    validate_settings(_settings(tmp_path, []))
    err = capsys.readouterr().err
    assert "deny_globs is empty" in err
    assert "vtai init" in err


def test_configured_deny_globs_do_not_warn(capsys, tmp_path):
    validate_settings(_settings(tmp_path, ["~/.ssh/**"]))
    assert capsys.readouterr().err == ""
