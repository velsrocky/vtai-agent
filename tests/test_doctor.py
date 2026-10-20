"""`vtai doctor` is the preflight a new user runs first: it must report
problems instead of raising them, and it must not lie about the machine it
is on."""

import tomllib
from pathlib import Path

import pytest

from vtai_agent import doctor


def test_min_uv_matches_the_declared_floor():
    """The floor is declared in pyproject.toml; the doctor must not drift
    from it."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("not running from a checkout")
    data = tomllib.loads(pyproject.read_text())
    required = data["tool"]["uv"]["required-version"]
    floor = tuple(int(p) for p in required.lstrip(">=").split("."))
    assert doctor.MIN_UV == floor


def test_doctor_reports_a_healthy_machine(capsys, sandbox, monkeypatch):
    monkeypatch.setattr(doctor, "_probe_url", lambda *a, **k: True)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "agent version" in out
    assert "config valid" in out
    assert "pending approvals" in out


def test_doctor_reports_a_broken_config_instead_of_raising(capsys, sandbox,
                                                           monkeypatch):
    from vtai_agent import config

    def boom():
        raise ValueError("invalid configuration:\n  - writable_roots entry does not exist: /nope")

    monkeypatch.setattr(config, "get_settings", boom)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "config valid" in out
    assert "writable_roots entry does not exist" in out


def test_doctor_survives_an_unreadable_db(capsys, sandbox, monkeypatch):
    from vtai_agent import db

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "list_pending_approvals", boom)
    monkeypatch.setattr(doctor, "_probe_url", lambda *a, **k: True)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert "pending approvals" in out
    assert rc in (0, 1)


def test_doctor_flags_a_missing_writable_root(capsys, sandbox, monkeypatch):
    from vtai_agent import config

    settings = sandbox.settings
    settings.paths.writable_roots = [Path("/nope/does/not/exist")]
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(doctor, "_probe_url", lambda *a, **k: True)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "writable roots" in out


def test_doctor_flags_an_old_uv(capsys, sandbox, monkeypatch):
    monkeypatch.setattr(doctor, "_uv_version", lambda: ("0.9.0", "uv 0.9.0"))
    monkeypatch.setattr(doctor, "_probe_url", lambda *a, **k: True)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "older than the required" in out


def test_probe_url_is_false_for_a_dead_port():
    assert doctor._probe_url("http://127.0.0.1:1") is False
