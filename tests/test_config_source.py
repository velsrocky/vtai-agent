"""The active config file must be discoverable: a user cloning the repo gets
the checkout's config, and `vtai info` has to say which file (and which
source) is in effect — otherwise their own settings look ignored."""

from pathlib import Path

from vtai_agent.config import config_source


def test_env_override_is_reported(monkeypatch):
    monkeypatch.setenv("VT_CONFIG", "/tmp/does-not-need-to-exist.toml")
    assert config_source() == "$VT_CONFIG"


def test_repo_adjacent_config_is_reported(monkeypatch):
    monkeypatch.delenv("VT_CONFIG", raising=False)
    repo_config = Path(__file__).resolve().parents[1] / "config" / "settings.toml"
    if repo_config.exists():
        assert config_source() == "repo-adjacent"
    else:
        assert config_source() == "user-level"


def test_user_level_when_no_repo_config(monkeypatch):
    """A non-editable install has no repo to be adjacent to."""
    monkeypatch.delenv("VT_CONFIG", raising=False)
    monkeypatch.setattr("vtai_agent.config.Path.exists", lambda self: False)
    assert config_source() == "user-level"


def test_info_names_the_config_file(capsys, sandbox):
    """`sandbox` pins the settings, so this does not depend on the
    machine's (or the checkout's) config validating."""
    from vtai_agent.cli import cmd_info

    cmd_info(None)
    out = capsys.readouterr().out
    assert "config file" in out
    assert ".toml (" in out  # path plus the source in parentheses


def test_info_reports_an_invalid_config_instead_of_crashing(capsys, monkeypatch):
    """`info` is the diagnostic command — a broken config is what the user
    came to find, so it must be reported, not raised."""
    from vtai_agent import cli, config

    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(
        config,
        "validate_settings",
        lambda s: (_ for _ in ()).throw(ValueError("invalid configuration:\n  - nope")),
    )

    rc = cli.cmd_info(None)
    captured = capsys.readouterr()
    assert rc == 1
    assert "INVALID" in captured.out
    assert "nope" in captured.err


def test_init_points_at_vt_config_for_repo_adjacent_config(capsys, monkeypatch,
                                                             tmp_path):
    """A fresh clone must not be left thinking it should overwrite the
    checkout's tracked config — the hint has to name the per-user path."""
    from vtai_agent import cli, config

    existing = tmp_path / "settings.toml"
    existing.write_text("active_provider = \"local\"\n")
    monkeypatch.setattr(config, "CONFIG_TOML", existing)
    monkeypatch.setattr(config, "config_source", lambda: "repo-adjacent")
    monkeypatch.setattr(config, "validate_settings", lambda s: None)
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)

    class Args:
        force = False

    cli.cmd_init(Args())
    out = capsys.readouterr().out
    assert "VT_CONFIG" in out
    assert "checkout's own config" in out


def test_init_note_absent_for_user_config(capsys, monkeypatch, tmp_path):
    from vtai_agent import cli, config

    existing = tmp_path / "settings.toml"
    existing.write_text("active_provider = \"local\"\n")
    monkeypatch.setattr(config, "CONFIG_TOML", existing)
    monkeypatch.setattr(config, "config_source", lambda: "user-level")
    monkeypatch.setattr(config, "validate_settings", lambda s: None)
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)

    class Args:
        force = False

    cli.cmd_init(Args())
    out = capsys.readouterr().out
    assert "checkout's own config" not in out


def test_config_toml_path_re_resolves(monkeypatch, tmp_path):
    """config_toml_path() must track VT_CONFIG live — CONFIG_TOML is frozen
    at import time and long-lived processes change the env after import."""
    from vtai_agent import config

    real = tmp_path / "live.toml"
    real.write_text('active_provider = "local"\n')
    monkeypatch.setenv("VT_CONFIG", str(real))
    assert config.config_toml_path() == real


def test_info_flags_missing_config_file(capsys, monkeypatch, sandbox, tmp_path):
    """A fresh machine before `vtai init`: the resolution names a user-level
    path that was never written. `info` must not dress that up as a normal
    config — the defaults have an empty writable_roots and the user sees
    only denials."""
    from vtai_agent import cli, config

    missing = tmp_path / "never-written.toml"
    monkeypatch.setattr(config, "CONFIG_TOML", missing)
    monkeypatch.setattr(config, "config_toml_path", lambda: missing)
    monkeypatch.setenv("VT_CONFIG", str(missing))
    monkeypatch.setattr(config, "_settings", sandbox.settings)

    rc = cli.cmd_info(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no file" not in out  # the label itself keeps its vocabulary
    assert "no config file there" in out
    assert "built-in defaults" in out
    assert "vtai init" in out


def test_info_no_warning_when_config_file_exists(capsys, monkeypatch, sandbox,
                                                 tmp_path):
    from vtai_agent import cli, config

    existing = tmp_path / "settings.toml"
    existing.write_text('active_provider = "local"\n')
    monkeypatch.setattr(config, "CONFIG_TOML", existing)
    monkeypatch.setattr(config, "config_toml_path", lambda: existing)
    monkeypatch.setattr(config, "_settings", sandbox.settings)

    cli.cmd_info(None)
    out = capsys.readouterr().out
    assert "no config file there" not in out


def test_init_next_steps_name_doctor_and_model_setup(capsys, monkeypatch,
                                                     tmp_path):
    """The README's quick start starts with `vtai doctor` (it is the check
    that catches an unconfigured model). Init's own footer must agree."""
    from vtai_agent import cli, config

    missing = tmp_path / "missing.toml"
    monkeypatch.setattr(config, "CONFIG_TOML", missing)
    monkeypatch.setattr(config, "config_source", lambda: "user-level")
    monkeypatch.setattr(config, "validate_settings", lambda s: None)
    monkeypatch.setattr(cli, "STARTER_TOML", "")
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)

    class Args:
        force = True  # write (or no-op) then print the footer
        doctor = False

    cli.cmd_init(Args())
    out = capsys.readouterr().out
    assert "vtai doctor" in out
    assert "vtai goal --dry-run" in out
    assert "VT_ACTIVE_PROVIDER" in out


def test_init_doctor_flag_runs_doctor(capsys, monkeypatch):
    from vtai_agent import cli, config

    monkeypatch.setattr(config, "validate_settings", lambda s: None)
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None)
    called = {}
    monkeypatch.setattr(cli, "cmd_doctor", lambda a: called.setdefault("hit", a))

    class Args:
        force = True
        doctor = True

    cli.cmd_init(Args())
    assert "hit" in called
    assert called["hit"].doctor is True


def test_init_doctor_skipped_when_init_fails(capsys, monkeypatch):
    """A failed validation must not launch doctor pretending success."""
    from vtai_agent import cli, config

    monkeypatch.setattr(
        config, "validate_settings", lambda s: (_ for _ in ()).throw(
            ValueError("invalid configuration:\n  - broken")))
    ran = {}
    monkeypatch.setattr(cli, "cmd_doctor", lambda a: ran.setdefault("hit", a))

    class Args:
        force = True
        doctor = True

    rc = cli.cmd_init(Args())
    assert rc == 1
    assert "hit" not in ran
