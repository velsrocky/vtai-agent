import asyncio
import os
import shutil
import socket
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
    return {"total_gb": total, "used_gb": used, "free_gb": free}


def disk_info() -> list[dict]:
    out: list[dict] = []
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mp, fst = parts[0], parts[1], parts[2]
        if fst in {"tmpfs", "proc", "sysfs", "cgroup", "devtmpfs", "squashfs", "overlay"}:
            continue
        if not mp.startswith(("/",)):
            continue
        st = shutil.disk_usage(mp if Path(mp).is_dir() else "/")
        out.append({
            "mount": mp, "fstype": fst, "device": dev,
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
                l.strip() for l in stdout.decode(errors="replace").splitlines()
                if "deviceName" in l
            ]
            return {"backend": "vulkan", "devices": devs[:4]}
        except (asyncio.TimeoutError, OSError):
            pass
    return {"backend": "none", "note": "no rocm-smi or vulkaninfo found"}
