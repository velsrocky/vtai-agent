import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import platform
from pathlib import Path

from pydantic import BaseModel, Field

from .registry import Tool, ToolResult


class SystemInfoInput(BaseModel):
    include_gpu: bool = Field(default=True, description="Probe AMD/NPU via rocm-smi when available")


class SystemInfoTool(Tool[SystemInfoInput]):
    name = "system_info"
    description = (
        "Read-only snapshot of this machine: CPU, memory, disk, load, and AMD GPU "
        "state. Safe to call any time; used to ground decisions before acting."
    )
    InputModel = SystemInfoInput

    async def run(self, inp: SystemInfoInput, *, run_id: int | None = None) -> ToolResult:
        info: dict = {
            "host": socket.gethostname(),
            "os": platform.platform(),
            "cpu": platform.processor() or "unknown",
            "cpu_count": os_cpu_count(),
            "load": os_loadavg(),
            "memory": memory_info(),
            "disks": disk_info(),
            "agent": agent_info(),
        }
        if inp.include_gpu:
            info["gpu"] = await gpu_info()
        return ToolResult(
            ok=True,
            summary=f"{info['host']}: {info['cpu_count']} CPUs, "
                    f"{info['memory']['total_gb']:.1f} GB RAM, "
                    f"{len(info['disks'])} mounts",
            detail=info,
        )


def agent_info() -> dict:
    """Which agent build and config is in effect, and what it may touch.

    The model driving this tool needs to know the boundaries it works
    inside: which config file the server loaded (a checkout's own config
    and the user's config are not the same thing), where it may write, and
    whether it is running dry.
    """
    from .. import __version__
    from ..config import CONFIG_TOML, config_source, error_summary, get_settings

    info: dict = {"version": __version__, "config_file": str(CONFIG_TOML),
                  "config_source": config_source()}
    try:
        s = get_settings()
    except ValueError as e:
        info["config_valid"] = False
        info["config_error"] = error_summary(e)
        return info
    info["config_valid"] = True
    info["dry_run_default"] = s.guardrails.dry_run_default
    info["writable_roots"] = [str(p) for p in s.paths.writable_roots]
    info["deny_globs"] = list(s.guardrails.deny_globs)
    info["shell_allowlist"] = list(s.guardrails.shell_allowlist)
    return info


def os_cpu_count() -> int:
    return os.cpu_count() or 0


def os_loadavg() -> list[float]:
    try:
        return list(os.getloadavg())
    except AttributeError:
        return []


def memory_info() -> dict:
    total = used = free = 0.0
    try:
        with open("/proc/meminfo") as f:
            fields: dict[str, float] = {}
            for line in f:
                key, rest = line.split(":", 1)
                fields[key.strip()] = float(rest.split()[0]) / 1024 ** 2  # MB->GB
        total = fields.get("MemTotal", 0.0)
        avail = fields.get("MemAvailable", 0.0)
        used = total - avail
        free = fields.get("MemFree", 0.0)
    except OSError:
        pass
    if total == 0.0 and sys.platform == "darwin":
        total, free = _darwin_memory()
        used = max(total - free, 0.0) if total else 0.0
    return {"total_gb": total, "used_gb": used, "free_gb": free}


def _darwin_memory() -> tuple[float, float]:
    """Total via sysctl, reclaimable via vm_stat. No /proc on macOS."""
    total = free = 0.0
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            total = int(out.stdout.strip()) / 1024 ** 3
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            page_size = 16384
            m = re.search(r"page size of (\d+) bytes", out.stdout)
            if m:
                page_size = int(m.group(1))
            pages: dict[str, int] = {}
            for line in out.stdout.splitlines():
                mm = re.match(r"Pages\s+(free|inactive|speculative):\s+(\d+)", line)
                if mm:
                    pages[mm.group(1)] = int(mm.group(2))
            free_pages = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative"))
            free = free_pages * page_size / 1024 ** 3
    except OSError:
        pass
    return total, free


def disk_info() -> list[dict]:
    try:
        text = Path("/proc/mounts").read_text()
    except OSError:
        return _disk_info_fallback()
    out: list[dict] = []
    seen_usage: set[tuple[int, int]] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mp, fst = parts[0], parts[1], parts[2]
        if not mp.startswith(("/",)):
            continue
        # Small stable exclusion by *storage semantics* (not an exhaustive
        # pseudo-fs list): tmpfs/devtmpfs live in RAM, overlay is a merged
        # view of the host disk (duplicate numbers), squashfs images are
        # read-only snap mounts. Everything else is decided by measurement.
        if fst in {"tmpfs", "devtmpfs", "overlay", "squashfs"}:
            continue
        try:
            st = shutil.disk_usage(mp if Path(mp).is_dir() else "/")
        except OSError:
            continue
        # Property-based filter, no fstype list to rot:
        # - zero size → pseudo/synthetic mount (proc, sysfs, cgroup*, …);
        # - sub-MB → firmware/config virtual filesystems (efivarfs, …);
        # - identical (total, free) → namespace/bind duplicate of an
        #   already-reported disk (snap nsfs, docker netns, …).
        if st.total < 1024 ** 2:
            continue
        usage = (st.total, st.free)
        if usage in seen_usage:
            continue
        seen_usage.add(usage)
        out.append({
            "mount": mp, "fstype": fst, "device": dev,
            "total_gb": st.total / 1024 ** 3,
            "used_gb": st.used / 1024 ** 3,
            "free_gb": st.free / 1024 ** 3,
        })
    return out


def _disk_info_fallback() -> list[dict]:
    """Portable fallback for macOS/BSD (no /proc/mounts): probe /, $HOME,
    and /Volumes/* directly."""
    fstype = "apfs" if sys.platform == "darwin" else "unknown"
    candidates = ["/", str(Path.home())]
    vols = Path("/Volumes")
    if vols.is_dir():
        try:
            candidates.extend(str(p) for p in vols.iterdir())
        except OSError:
            pass
    out: list[dict] = []
    seen: set[str] = set()
    seen_usage: set[tuple[int, int]] = set()
    for mp in candidates:
        try:
            p = Path(mp)
            if not p.is_dir():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            st = shutil.disk_usage(str(p))
        except OSError:
            continue
        # APFS volumes in one container share space (/ and /Users report
        # identical numbers). Collapse exact duplicates so the mount count
        # stays meaningful; the first (shortest) path wins.
        usage = (st.total, st.free)
        if usage in seen_usage:
            continue
        seen_usage.add(usage)
        out.append({
            "mount": mp, "fstype": fstype, "device": mp,
            "total_gb": st.total / 1024 ** 3,
            "used_gb": st.used / 1024 ** 3,
            "free_gb": st.free / 1024 ** 3,
        })
    return out


async def gpu_info() -> dict:
    for binary in ("/opt/rocm/bin/rocm-smi", "rocm-smi"):
        if shutil.which(binary) or Path(binary).exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "--showproductname", "--showuse", "--showmeminfo", "vram",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                return {"backend": "rocm", "raw": stdout.decode(errors="replace")[:800]}
            except (asyncio.TimeoutError, OSError):
                break
    if shutil.which("vulkaninfo"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "vulkaninfo", "--summary",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            devs = [
                ln.strip() for ln in stdout.decode(errors="replace").splitlines()
                if "deviceName" in ln
            ]
            return {"backend": "vulkan", "devices": devs[:4]}
        except (asyncio.TimeoutError, OSError):
            pass
    if sys.platform == "darwin":
        return {"backend": "metal", "note": "Metal GPU (no rocm-smi/vulkaninfo on macOS)"}
    return {"backend": "none", "note": "no rocm-smi or vulkaninfo found"}
