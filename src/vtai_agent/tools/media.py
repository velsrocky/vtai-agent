"""Media transcoding — Tier A workflow #3.

Batch video transcoding on the AMD GPU via VAAPI. Hard safety rule: output
never lands beside the source — it goes into a dedicated subdirectory that is
itself skipped as a source, so the agent cannot re-encode its own output or
clobber originals. Falls back to software if the VAAPI device is unavailable.
"""

import asyncio
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..guardrails import Guardrails
from .registry import Tool, ToolResult

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
              ".mpg", ".mpeg", ".ts", ".3gp"}


class TranscodeMediaInput(BaseModel):
    directory: str = Field(description="Directory containing videos to transcode (searched recursively).")
    dry_run: bool | None = Field(default=None, description="Report what would be transcoded; encode nothing.")
    codec: str | None = Field(default=None, description="Encoder: h264_vaapi | hevc_vaapi | av1_vaapi | libx264 (defaults to config).")
    crf: int | None = Field(default=None, ge=0, le=51, description="Quality for software codecs (lower = better).")
    recursive: bool = Field(default=True, description="Descend into subdirectories.")
    overwrite: bool = Field(default=False, description="Re-encode even if an output already exists.")


class MediaTranscodeTool(Tool[TranscodeMediaInput]):
    name = "transcode_media"
    description = (
        "Batch transcode videos with AMD GPU hardware acceleration (VAAPI). "
        "Outputs go to a 'transcoded' subfolder and originals are never modified. "
        "Automatically skips its own output directory to avoid re-encoding loops."
    )
    InputModel = TranscodeMediaInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()
        self.cfg = get_settings().media

    async def run(self, inp: TranscodeMediaInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        root = self.g.check_readable(inp.directory)
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")
        self.g.check_writable(root)  # output subdir lives inside root

        codec = inp.codec or self.cfg.video_codec
        out_name = self.cfg.output_dir_name
        candidates = list(root.rglob("*")) if inp.recursive else list(root.iterdir())
        jobs: list[dict] = []
        for f in candidates:
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            if out_name in f.parts:  # never re-encode our own output
                continue
            out_dir = f.parent / out_name
            dest = out_dir / f"{f.stem}.{self.cfg.container}"
            if dest.exists() and not inp.overwrite:
                continue
            jobs.append({"source": str(f), "destination": str(dest),
                         "size_bytes": f.stat().st_size})

        if dry_run:
            db.audit(run_id, "transcode", f"[dry-run] {len(jobs)} videos in {root}",
                     allowed=True)
            return ToolResult(
                ok=True,
                summary=f"DRY RUN: would transcode {len(jobs)} videos "
                        f"({sum(j['size_bytes'] for j in jobs) / 1e6:.1f} MB) with {codec}",
                detail={"codec": codec, "container": self.cfg.container,
                        "jobs": jobs, "dry_run": True},
                dry_run=True,
            )

        hw = self._vaapi_available()
        results = []
        ok = fail = 0
        for j in jobs:
            dest = Path(j["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            argv = self._build_argv(j["source"], str(dest), codec, hw, inp.crf)
            db.audit(run_id, "ffmpeg", " ".join(argv), allowed=True)
            try:
                await self._run_ffmpeg(argv)
                ok += 1
                results.append({"source": j["source"], "dest": str(dest), "ok": True,
                                "hw": hw})
            except Exception as e:
                fail += 1
                results.append({"source": j["source"], "dest": str(dest), "ok": False,
                                "error": str(e), "hw": hw})

        return ToolResult(
            ok=fail == 0,
            summary=f"Transcoded {ok} videos with {'VAAPI GPU' if hw else 'software'} "
                    f"({fail} failed) in {root}",
            detail={"codec": codec, "container": self.cfg.container,
                    "hardware": hw, "ok": ok, "failed": fail, "results": results},
        )

    def _vaapi_available(self) -> bool:
        dev = Path(self.cfg.vaapi_device)
        return dev.exists() and self.cfg.video_codec.endswith("_vaapi")

    def _build_argv(self, src: str, dst: str, codec: str, hw: bool,
                    crf: int | None) -> list[str]:
        if hw and codec.endswith("_vaapi"):
            return [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-init_hw_device", f"vaapi=vaapi:{self.cfg.vaapi_device}",
                "-filter_hw_device", "vaapi",
                "-i", src,
                "-vf", "format=nv12,hwupload",
                "-c:v", codec,
                dst,
            ]
        # software fallback
        sw = codec if not codec.endswith("_vaapi") else "libx264"
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                "-c:v", sw]
        if crf is not None:
            args += ["-crf", str(crf)]
        return args + [dst]

    async def _run_ffmpeg(self, argv: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=7200)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exit {proc.returncode}: "
                               f"{err.decode(errors='replace')[-300:]}")
