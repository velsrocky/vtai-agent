"""Portability tests for system_info: must work without /proc (macOS/BSD)."""

import asyncio
import sys

from vtai_agent.tools.system_info import (
    SystemInfoInput,
    SystemInfoTool,
    disk_info,
    memory_info,
)


def test_memory_info_shape():
    mem = memory_info()
    assert mem["total_gb"] >= 0.0
    assert mem["used_gb"] >= 0.0
    assert mem["free_gb"] >= 0.0


def test_disk_info_does_not_crash_without_proc():
    disks = disk_info()
    assert isinstance(disks, list)
    for d in disks:
        assert {"mount", "fstype", "device", "total_gb", "used_gb", "free_gb"} <= set(d)
        assert d["total_gb"] > 0


def test_system_info_tool_runs_end_to_end():
    r = asyncio.run(SystemInfoTool().run(SystemInfoInput()))
    assert r.ok
    assert r.detail["memory"]["total_gb"] >= 0.0
    assert isinstance(r.detail["disks"], list)
    if sys.platform == "darwin":
        assert r.detail["memory"]["total_gb"] > 0
        assert r.detail["gpu"]["backend"] == "metal"


def test_system_info_reports_the_config_and_its_boundaries(sandbox):
    """The model driving the agent needs to know which config is in effect
    and what it may touch."""
    r = asyncio.run(SystemInfoTool().run(SystemInfoInput(include_gpu=False)))
    agent = r.detail["agent"]
    assert agent["config_valid"] is True
    assert agent["config_file"].endswith(".toml")
    assert agent["config_source"] in ("$VT_CONFIG", "repo-adjacent", "user-level")
    assert agent["writable_roots"]
    assert agent["deny_globs"]
    assert agent["dry_run_default"] is True


def test_agent_info_survives_a_broken_config(sandbox, monkeypatch):
    from vtai_agent import config
    from vtai_agent.tools.system_info import agent_info

    def boom():
        raise ValueError("invalid configuration:\n  - nope")

    monkeypatch.setattr(config, "get_settings", boom)
    info = agent_info()
    assert info["config_valid"] is False
    assert "nope" in info["config_error"]
