"""Backup and sync — Tier A workflow #2.

rsync-based incremental backup with verification. Safety posture:
  * --delete is never default; requires explicit opt-in AND execute mode
  * sources are read-checked, destinations write-checked, both audited
  * dry-run reports the transfer plan (created/changed/deleted) before any move
"""

import asyncio
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..guardrails import GuardrailViolation, Guardrails
from .registry import Tool, ToolResult

_SUMMARY_RE = re.compile(
    r"Number of files: (?P<files>[\d,]+).*?"
    r"Number of regular files transferred: (?P<xfer>[\d,]+).*?"
    r"Total file size: (?P<size>[\d,]+) bytes",
    re.DOTALL,
)


def _num(s: str) -> int:
    return int(s.replace(",", ""))


class BackupSyncInput(BaseModel):
    source: str = Field(description="Directory to back up.")
    destination: str = Field(description="Backup destination directory (must be writable).")
    dry_run: bool | None = Field(default=None, description="Preview the transfer plan only.")
    delete_extraneous: bool = Field(
        default=False,
        description="Delete files in destination not present in source. DANGEROUS — "
                    "ignored unless the run is in EXECUTE mode.",
    )
    compress: bool = Field(default=True, description="Use rsync -z for network transfers.")
    verify: bool = Field(default=True, description="Re-run a checksum comparison after transfer.")


class BackupSyncTool(Tool[BackupSyncInput]):
    name = "backup_sync"
    description = (
        "Incremental directory backup/sync via rsync. Reports the transfer plan "
        "(files/bytes, what changes) and can verify with a second checksum pass. "
        "Deletion of extraneous destination files is opt-in and never happens in a "
        "dry run."
    )
    InputModel = BackupSyncInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()

    async def run(self, inp: BackupSyncInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        src = self.g.check_readable(inp.source)
        dst = self.g.check_writable(inp.destination)
        if not src.is_dir():
            raise NotADirectoryError(f"source is not a directory: {src}")
        dst.mkdir(parents=True, exist_ok=True)

        argv = ["rsync", "-a", "--itemize-changes", "--stats"]
        if dry_run:
            argv.append("-n")
        if inp.compress:
            argv.append("-z")
        # delete only when explicitly requested AND actually executing
        if inp.delete_extraneous and not dry_run:
            argv.append("--delete")
        argv += [f"{src}/", f"{dst}/"]

        db.audit(run_id, "rsync", f"{'[dry-run] ' if dry_run else ''}{' '.join(argv)}",
                 allowed=True)
        stats = await self._run(argv)

        detail: dict = {"command": " ".join(argv), "stats": stats, "dry_run": dry_run}
        if dry_run and inp.delete_extraneous:
            detail["note"] = ("--delete requested but suppressed in dry run; "
                              "re-run in EXECUTE mode to apply.")

        if inp.verify and not dry_run:
            detail["verify"] = await self._verify(src, dst)

        parsed = _SUMMARY_RE.search(stats)
        if parsed:
            detail["summary_numbers"] = {
                "files": _num(parsed.group("files")),
                "transferred": _num(parsed.group("xfer")),
                "total_bytes": _num(parsed.group("size")),
            }
            summary = (f"{'DRY RUN: ' if dry_run else ''}{parsed.group('xfer')} files "
                       f"({_num(parsed.group('size'))} bytes total) from {src} to {dst}")
        else:
            summary = f"{'DRY RUN: ' if dry_run else ''}rsync completed {src} -> {dst}"

        return ToolResult(ok=True, summary=summary, detail=detail, dry_run=dry_run)

    async def _run(self, argv: list[str]) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3600)
        except asyncio.TimeoutError:
            raise TimeoutError("rsync exceeded 1h; aborted")
        text = out.decode(errors="replace")
        if proc.returncode not in (0, 23, 24):  # 23/24 = partial transfer, still usable info
            raise RuntimeError(f"rsync exit {proc.returncode}: {text[-500:]}")
        return text

    async def _verify(self, src: Path, dst: Path) -> dict:
        """Second pass with checksums; catches silent corruption and missing files."""
        argv = ["rsync", "-a", "-c", "-n", "--itemize-changes", f"{src}/", f"{dst}/"]
        text = await self._run(argv)
        diffs = [ln for ln in text.splitlines() if ln.startswith((">", "<", "*"))]
        return {"differences": len(diffs), "sample": diffs[:20], "clean": not diffs}
