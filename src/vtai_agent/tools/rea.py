"""inspect_application — run REA (github.com/morluto/rea) under guardrails.

REA gives an agent deep static analysis of JavaScript/Electron apps and
native binaries. That power is exactly why the capability stays OFF until it
is explicitly enabled in settings. Safety contract, in order:

  1. The capability must be manually enabled (`[capabilities.rea] enabled`,
     default false) — get_settings fails fast before that.
  2. The target passes the deny-glob check, then is COPIED into a quarantine
     directory under data_dir. REA never receives the original path, and
     entries matching deny_globs (or dangling/out-of-policy symlinks) are
     skipped on the way in.
  3. The REA binary must be the configured bare name, PATH-resolved at call
     time. argv is the validated command_prefix + copy path + `--json`; no
     shell resolves anything in between.
  4. The child gets a minimal environment (no inherited API keys), and its
     work directory is the quarantine dir, so any incidental writes stay in
     data_dir.
  5. Output JSON is parsed; a sha256 digest plus the evidence id land in the
     audit trail (detail of the run's audit row).
  6. The copy is deleted after the run unless keep_copy is requested.
"""

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import db
from ..config import CapabilityRea, Settings, get_settings
from ..guardrails import Guardrails
from .automode import AutoModeTool
from .registry import Tool, ToolError, ToolResult


def _child_env() -> dict[str, str]:
    """Minimal environment, same policy as run_shell: children never inherit
    API keys or tokens."""
    keep = ("LC_ALL", "TZ", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home()),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    for k in keep:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


class InspectApplicationInput(BaseModel):
    goal: str = Field(
        description="What you want to learn, e.g. 'explain how the CSV "
                    "export builds its header row'.",
    )
    target: str = Field(
        description="Path to the app to analyze (directory, ASAR, or binary). "
                    "It is copied into quarantine first; the original is "
                    "never touched.",
    )
    judge: bool = Field(
        default=True,
        description="Review REA's evidence against the goal with the "
                    "configured model before reporting success (same "
                    "verify-then-trust policy as auto_mode).",
    )
    keep_copy: bool | None = Field(
        default=None,
        description="Keep the quarantine copy after the run (defaults to "
                    "config capabilities.rea.keep_copy).",
    )
    timeout: float | None = Field(
        default=None,
        description="Seconds before REA is killed (defaults to config "
                    "capabilities.rea.timeout).",
    )
    dry_run: bool | None = Field(
        default=None,
        description="Report the planned copy dir and argv without running "
                    "REA. Defaults to config dry_run_default.",
    )


class InspectApplicationTool(Tool[InspectApplicationInput]):
    name = "inspect_application"
    description = (
        "Analyze an application with REA (static reverse engineering) under "
        "full guardrails: deny-glob check, quarantine copy, minimal child "
        "environment, evidence digest in the audit trail, optional judge pass "
        "against the goal. The REA capability must be enabled in config."
    )
    InputModel = InspectApplicationInput

    def __init__(self, guardrails: Guardrails | None = None,
                 settings: Settings | None = None, model=None):
        self.g = guardrails or Guardrails()
        self.settings = settings or get_settings()
        self._model = model

    async def run(self, inp: InspectApplicationInput, *,
                  run_id: int | None = None) -> ToolResult:
        cfg = self.settings.capabilities.rea
        if not cfg.enabled:
            raise ToolError(
                "REA capability is disabled in config; enable it with "
                "[capabilities.rea] enabled=true first")
        resolved, argv_prefix = self._resolve_command(cfg)

        target = self.g.check_readable(inp.target)
        if not target.exists():
            raise ToolError(f"target does not exist: {inp.target}")

        timeout = float(inp.timeout if inp.timeout is not None else cfg.timeout)
        plan = {
            "goal": inp.goal[:200],
            "evaluated_target": str(target),
            "resolve_path": resolved,
            "timeout": timeout,
            "judge": inp.judge,
        }
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        if dry_run:
            db.audit(run_id, "rea", f"[dry-run] {inp.goal[:120]}", allowed=True)
            return ToolResult(
                ok=True,
                summary="DRY RUN: plan ready (would copy target into quarantine "
                        "and run REA on the copy)",
                detail={**plan, "argv_prefix": argv_prefix, "dry_run": True},
                dry_run=True,
            )

        self.g.ensure_not_killed()
        job = f"r-{uuid.uuid4().hex[:12]}"
        qdir = Path(self.settings.data_dir) / "quarantine" / job
        qdir.mkdir(parents=True, exist_ok=True)
        stats = self._copy_into_quarantine(target, qdir)
        plan["quarantine"] = str(qdir)
        plan["copy"] = stats
        db.audit(run_id, "rea",
                 f"quarantined {stats['files']} entries of '{target.name}' "
                 f"into {qdir}", allowed=True)

        full_argv = [resolved, *argv_prefix, str(qdir / target.name), "--json"]
        try:
            stdout, stderr, code = await self._run(full_argv, qdir, timeout)
        except asyncio.TimeoutError:
            self._cleanup(qdir, inp, cfg)
            return ToolResult(
                ok=False,
                summary=f"REA timed out after {timeout}s; quarantine copy removed",
                detail={**plan, "returncode": -1, "reason": "timeout"},
            )
        report = self._parse(stdout)
        digest = f"sha256:{hashlib.sha256(stdout.encode()).hexdigest()[:16]}"
        detail: dict[str, Any] = {
            **plan,
            "argv": full_argv,
            "returncode": code,
            "stderr": stderr[-2000:],
            "evidence_digest": digest,
            "evidence_id": self._evidence_id(report),
            "normalized_result": self._trim(report),
        }
        db.audit(run_id, "rea", f"REA exited {code}; evidence {digest}",
                 allowed=True)
        detail["quarantine_kept"] = inp.keep_copy if inp.keep_copy is not None else cfg.keep_copy

        if code != 0:
            reason = f"REA exited {code}: {(stdout or stderr)[-300:]}"
            self._cleanup(qdir, inp, cfg)
            return ToolResult(ok=False, summary=reason, detail=detail)

        if inp.judge:
            verdict, critique = await self._judge(inp.goal, report)
            detail["judge"] = {"verdict": verdict, "critique": critique[:1000]}
            db.audit(run_id, "rea", f"judge verdict {verdict}", allowed=True)
            if verdict == "error":
                self._cleanup(qdir, inp, cfg)
                return ToolResult(ok=False,
                                  summary=f"cannot verify evidence against the goal: {critique}",
                                  detail=detail)
            if verdict == "insufficient":
                self._cleanup(qdir, inp, cfg)
                return ToolResult(ok=False,
                                  summary=f"evidence did not satisfy the goal: {critique}",
                                  detail=detail)

        self._cleanup(qdir, inp, cfg)
        return ToolResult(
            ok=True,
            summary=f"REA analysis of '{target.name}' complete; evidence {digest}",
            detail=detail,
        )

    # ---------- helpers ----------

    def _resolve_command(self, cfg: CapabilityRea) -> tuple[str, list[str]]:
        """Fail-closed resolution, re-checked at call time: bare binary name,
        PATH-resolved, and a command prefix free of paths, whitespace, and
        metacharacters (mirrors validate_settings, since a tool instance may
        outlive a settings edit)."""
        if os.sep in cfg.binary or "\\" in cfg.binary:
            raise ToolError(f"REA binary must be a bare name, not a path: {cfg.binary!r}")
        for tok in cfg.command_prefix:
            if (not tok or any(c.isspace() for c in tok) or ".." in tok
                    or any(c in tok for c in ";&|<>`$")):
                raise ToolError(f"REA command token rejected: {tok!r}")
        resolved = shutil.which(cfg.binary)
        if resolved is None:
            raise ToolError(
                f"'{cfg.binary}' not found on PATH; install REA only from the "
                "official sources (github.com/morluto/rea, "
                "`npm install --global rea-agents`), then retry")
        return resolved, [*cfg.command_prefix]

    def _copy_into_quarantine(self, target: Path, qdir: Path) -> dict[str, int]:
        """Copy the target into qdir, skipping anything that did not itself
        pass the deny check — the analyzer never sees a path the agent may
        not read."""
        assert target.exists()
        stats = {"files": 0, "skipped_denied": 0, "skipped_broken": 0, "skipped_special": 0}
        if target.is_file():
            try:
                shutil.copy2(target, qdir / target.name)
                stats["files"] = 1
            except OSError:
                stats["skipped_special"] += 1
            return stats
        dst_root = qdir / target.name
        dst_root.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(target, followlinks=False):
            rel = Path(root).relative_to(target)
            (dst_root / rel).mkdir(parents=True, exist_ok=True)
            for d in list(dirs):
                src = Path(root) / d
                if src.is_symlink():
                    dirs.remove(d)
                    if not self._safe_link(src, dst_root / rel / d):
                        stats["skipped_broken"] += 1
                elif self.g.is_denied(src):
                    dirs.remove(d)
                    stats["skipped_denied"] += 1
            for f in files:
                src = Path(root) / f
                if src.is_symlink():
                    if not self._safe_link(src, dst_root / rel / f):
                        stats["skipped_broken"] += 1
                elif self.g.is_denied(src.resolve()):
                    stats["skipped_denied"] += 1
                elif not src.is_file():
                    stats["skipped_special"] += 1
                else:
                    try:
                        shutil.copy2(src, dst_root / rel / f)
                        stats["files"] += 1
                    except OSError:
                        stats["skipped_special"] += 1
        return stats

    def _safe_link(self, src: Path, dst: Path) -> bool:
        """Keep the link inside the copy only when its resolved target also
        passes the deny check — a symlink into ~/.ssh must not enter the
        analysis copy."""
        try:
            resolved = src.resolve()
        except OSError:
            return False
        if self.g.is_denied(resolved):
            return False
        try:
            link = src.readlink()
        except (OSError, ValueError):
            link = str(resolved)
        try:
            dst.symlink_to(link, target_is_directory=resolved.is_dir())
            return True
        except OSError:
            return False

    async def _run(self, argv: list[str], cwd: Path, timeout: float):
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode

    @staticmethod
    def _parse(stdout: str) -> dict[str, Any]:
        try:
            data = json.loads(stdout)
            return data if isinstance(data, dict) else {"value": data}
        except (json.JSONDecodeError, TypeError):
            return {"raw": stdout[-4000:]}

    @staticmethod
    def _evidence_id(report: dict[str, Any]) -> str | None:
        """REA's CLI and MCP shapes differ slightly; be tolerant about where
        the evidence id sits."""
        for k in ("evidence_id", "id"):
            if isinstance(report.get(k), str):
                return report[k]
        sc = report.get("structuredContent")
        if isinstance(sc, dict) and isinstance(sc.get("evidence_id"), str):
            return sc["evidence_id"]
        return None

    @staticmethod
    def _trim(report: dict[str, Any]) -> dict[str, Any]:
        """Keep the result payload inside MCP text limits; the full stdout is
        digest + audited instead of re-shipped. The `raw` fallback (non-JSON
        output) survives as-is since _parse already caps it."""
        keep = dict(report)
        text = json.dumps(keep, ensure_ascii=False)
        if len(text) > 8000:
            return {"excerpt": text[:8000], "truncated": True}
        return keep

    def _cleanup(self, qdir: Path, inp: InspectApplicationInput,
                 cfg: CapabilityRea) -> None:
        keep = inp.keep_copy if inp.keep_copy is not None else cfg.keep_copy
        if not keep and qdir.exists():
            shutil.rmtree(qdir, ignore_errors=True)

    async def _judge(self, goal: str, report: dict[str, Any]):
        """Reuse auto_mode's judge policy verbatim: same model, same retry and
        timeout bounds, same fail-closed verdict semantics."""
        normalized = report.get("normalized_result", report)
        reply = json.dumps(normalized, ensure_ascii=False) if not report.get("raw") else report["raw"]
        tool = AutoModeTool(self.g, self.settings, model=self._model)
        return await tool._judge(goal, reply[:4000])
