"""voice-sync: helpers install from versioned sources, drift is detectable."""

import argparse

from vtai_agent import cli


def _write(src, name, text):
    (src / name).write_text(text)


def test_voice_sync_installs_and_reports_unchanged(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "bin"
    src.mkdir()
    for n in cli.VOICE_HELPERS:
        _write(src, n, f"#!/bin/sh\necho {n}\n")
    _, in_sync = cli.voice_sync(src, dest, check=True)
    assert not in_sync
    states, in_sync = cli.voice_sync(src, dest)
    assert in_sync
    assert all(v == "installed" for v in states.values())
    states, in_sync = cli.voice_sync(src, dest)
    assert in_sync
    assert all(v == "unchanged" for v in states.values())


def test_voice_sync_updates_drifted_helper(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "bin"
    src.mkdir()
    for n in cli.VOICE_HELPERS:
        _write(src, n, "v1")
    cli.voice_sync(src, dest)
    _write(src, cli.VOICE_HELPERS[0], "v2")
    states, in_sync = cli.voice_sync(src, dest, check=True)
    assert not in_sync
    assert states[cli.VOICE_HELPERS[0]] == "updated"
    states, in_sync = cli.voice_sync(src, dest)
    assert in_sync
    assert states[cli.VOICE_HELPERS[0]] == "updated"
    assert (dest / cli.VOICE_HELPERS[0]).read_text() == "v2"


def test_voice_sync_check_fails_on_missing(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "bin"
    src.mkdir()
    for n in cli.VOICE_HELPERS:
        _write(src, n, "x")
    _, in_sync = cli.voice_sync(src, dest, check=True)
    assert not in_sync


def test_voice_sync_cmd_check_exit_code(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src"
    src.mkdir()
    for n in cli.VOICE_HELPERS:
        _write(src, n, "x")
    monkeypatch.setenv("VTAI_SCRIPTS_DIR", str(src))
    args = argparse.Namespace(dest=str(tmp_path / "bin"), check=True)
    assert cli.cmd_voice_sync(args) == 1
    capsys.readouterr()
    args = argparse.Namespace(dest=str(tmp_path / "bin"), check=False)
    assert cli.cmd_voice_sync(args) == 0
    args = argparse.Namespace(dest=str(tmp_path / "bin"), check=True)
    assert cli.cmd_voice_sync(args) == 0
