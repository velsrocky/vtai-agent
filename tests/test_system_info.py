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
