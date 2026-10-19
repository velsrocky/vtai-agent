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


def test_info_names_the_config_file(capsys):
    from vtai_agent.cli import cmd_info

    cmd_info(None)
    out = capsys.readouterr().out
    assert "config file" in out
    assert ".toml (" in out  # path plus the source in parentheses


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
