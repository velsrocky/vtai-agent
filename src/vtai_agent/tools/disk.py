"""Disk audit — read-only cleanup finder.

Never deletes anything. It reports the largest and oldest files plus the size of
common cache directories, giving the agent (or the operator) the facts needed to
decide what to reclaim. Any actual deletion must go through a guarded tool.
"""

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field

from ..guardrails import Guardrails
from .registry import Tool, ToolResult

CACHE_DIRS = [
    "~/.cache", "~/.local/share/Trash", "~/Downloads", "/tmp", "/var/cache/apt",
    "~/.npm", "~/.nuget", "~/.gradle", "~/.m2", "~/.cargo/registry",
    "~/.local/share/containers", "~/.mozilla", "~/.config/google-chrome",
    "~/.config/opencode", "~/.ollama/models", "~/models",
]


class DiskAuditInput(BaseModel):
    path: str = Field(default="/", description="Root to scan.")
    min_size_mb: int = Field(default=100, ge=1, description="Only report files at least this large.")
    older_than_days: int = Field(default=30, ge=0, description="Only report files older than this.")
    limit: int = Field(default=25, ge=1, le=200, description="Max files to report.")
    include_caches: bool = Field(default=True, description="Also measure known cache directories.")


class DiskAuditTool(Tool[DiskAuditInput]):
    name = "disk_audit"
    description = (
        "Read-only scan for reclaimable space: largest and oldest files, plus sizes "
        "of common cache directories. Deletes nothing — use it to decide what to "
        "clean, then clean with a guarded tool."
    )
    InputModel = DiskAuditInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()

    async def run(self, inp: DiskAuditInput, *, run_id: int | None = None) -> ToolResult:
        root = self.g.check_readable(inp.path)
        cutoff = time.time() - inp.older_than_days * 86400
        min_bytes = inp.min_size_mb * 1024 * 1024

        big = await self._scan(root, min_bytes, cutoff, inp.limit)
        caches = await self._caches() if inp.include_caches else []
        total_big = sum(f["size_bytes"] for f in big)

        return ToolResult(
            ok=True,
            summary=f"{len(big)} files >= {inp.min_size_mb} MB older than "
                    f"{inp.older_than_days}d ({total_big / 1e9:.1f} GB) under {root}",
            detail={
                "root": str(root),
                "min_size_mb": inp.min_size_mb,
                "older_than_days": inp.older_than_days,
                "largest_files": big,
                "cache_dirs": caches,
                "reclaimable_gb": (total_big + sum(c["size_bytes"] for c in caches)) / 1e9,
            },
        )

    async def _scan(self, root: Path, min_bytes: int, cutoff: float, limit: int) -> list[dict]:
        found: list[dict] = []

        def walk():
            for dirpath, dirnames, filenames in _walk_guarded(root):
                for name in filenames:
                    p = Path(dirpath) / name
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if st.st_size >= min_bytes and st.st_mtime < cutoff:
                        found.append({
                            "path": str(p), "size_bytes": st.st_size,
                            "modified": st.st_mtime,
                        })

        await asyncio.to_thread(walk)
        found.sort(key=lambda f: f["size_bytes"], reverse=True)
        return found[:limit]

    async def _caches(self) -> list[dict]:
        out: list[dict] = []

        def measure(d: Path) -> int:
            total = 0
            try:
                for f in d.rglob("*"):
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
            return total

        for c in CACHE_DIRS:
            d = Path(c).expanduser()
            if not d.is_dir():
                continue
            size = await asyncio.to_thread(measure, d)
            if size:
                out.append({"path": str(d), "size_bytes": size})
        out.sort(key=lambda c: c["size_bytes"], reverse=True)
        return out


def _walk_guarded(root: Path):
    """os.walk that skips unreadable dirs and symlink loops."""
    import os
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            st = d.stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        dirs, files = [], []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    dirs.append(Path(e.path))  # DirEntry.path is str — wrap it
                else:
                    files.append(e.name)
            except OSError:
                continue
        yield d, dirs, files
        stack.extend(dirs)
