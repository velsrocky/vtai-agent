"""CLI delegation — hand coding work to opencode / claude / pi headlessly.

The agent can now say "pi, refactor this module" and get a real diff back.
Safety contract, in order:
  1. The target must be a git repo we can snapshot (rollback guarantee).
  2. The delegate runs non-interactively with a hard timeout.
  3. The diff is captured for the audit log.
  4. If `verify_command` is given, it must pass — otherwise we auto-rollback.
  5. `--allow-dangerously-skip-permissions` is only used when explicitly requested;
     without it the CLIs may prompt and time out in unattended mode.
"""

import asyncio
import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..guardrails import GuardrailViolation, Guardrails
from .registry import Tool, ToolResult

CLI_BINARY = {
    "opencode": "opencode",
    "claude": "claude",
    "pi": "pi",
}


class DelegateCodingInput(BaseModel):
    cli: str = Field(description="Which coding agent to delegate to: opencode | claude | pi")
    prompt: str = Field(description="The coding task, e.g. 'Fix the off-by-one in parser.py'.")
    working_dir: str = Field(description="Project directory to run in (must be a git repo).")
    dry_run: bool | None = Field(default=None, description="Snapshot and show the plan; do not run the delegate.")
    verify_command: str | None = Field(
        default=None,
        description="Command that must pass after the edit (e.g. 'python -m pytest'). "
                    "Failure triggers rollback.",
    )
    auto_rollback: bool = Field(default=True, description="Restore the git snapshot if verification fails.")
    timeout: float = Field(default=600.0, ge=10, le=3600, description="Seconds before the delegate is killed.")
    skip_permissions: bool = Field(
        default=False,
        description="Allow --dangerously-skip-permissions so the CLI runs fully unattended. "
                    "Only use on trusted, isolated projects.",
    )


class DelegateCodingTool(Tool[DelegateCodingInput]):
    name = "delegate_coding"
    description = (
        "Delegate a coding task to opencode, claude, or pi in headless mode. "
        "Snapshots the repo first, captures the resulting diff, optionally verifies, "
        "and rolls back automatically if verification fails."
    )
    InputModel = DelegateCodingInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()

    async def run(self, inp: DelegateCodingInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        if inp.cli not in CLI_BINARY:
            raise ValueError(f"unknown cli {inp.cli!r}; choose from {list(CLI_BINARY)}")
        binary = CLI_BINARY[inp.cli]
        if shutil.which(binary) is None:
            raise FileNotFoundError(f"{binary} is not installed")

        work = self.g.check_writable(inp.working_dir)
        if not (work / ".git").is_dir():
            raise NotADirectoryError(f"not a git repo (no .git): {work}")

        await self._git(work, "add", "-A")
        snapshot_ok = await self._git(work, "stash", "create")
        snapshot = (snapshot_ok or "").strip()

        plan = {
            "cli": inp.cli, "working_dir": str(work), "timeout": inp.timeout,
            "verify_command": inp.verify_command, "auto_rollback": inp.auto_rollback,
        }
        if dry_run:
            db.audit(run_id, "delegate", f"[dry-run] {inp.cli}: {inp.prompt[:120]}",
                     allowed=True)
            return ToolResult(
                ok=True,
                summary=f"DRY RUN: would delegate to {inp.cli} in {work}",
                detail={"plan": plan, "prompt": inp.prompt, "dry_run": True,
                        "has_snapshot": bool(snapshot)},
                dry_run=True,
            )

        argv = self._build_argv(binary, inp)
        db.audit(run_id, "delegate", f"{inp.cli}: {inp.prompt[:120]}", allowed=True)
        try:
            stdout, stderr, code = await self._run(argv, work, inp.timeout)
        except asyncio.TimeoutError:
            await self._restore(work, snapshot, run_id, "timeout")
            return ToolResult(ok=False, summary=f"{inp.cli} timed out after {inp.timeout}s; rolled back",
                              detail={**plan, "rolled_back": True, "reason": "timeout"})

        diff = await self._git(work, "diff", "--stat")
        result = {
            **plan, "returncode": code, "diff": diff[-4000:],
            "stdout": stdout[-4000:], "stderr": stderr[-2000:],
        }

        if code != 0:
            if inp.auto_rollback:
                await self._restore(work, snapshot, run_id, f"exit {code}")
                result["rolled_back"] = True
                result["reason"] = f"delegate exited {code}"
            return ToolResult(ok=False, summary=f"{inp.cli} exited {code}", detail=result)

        # verification gate
        if inp.verify_command:
            v_ok, v_out = await self._verify(inp.verify_command, work)
            result["verify"] = {"ok": v_ok, "output": v_out[-2000:]}
            if not v_ok and inp.auto_rollback:
                await self._restore(work, snapshot, run_id, "verify failed")
                result["rolled_back"] = True
                result["reason"] = "verification failed"
                return ToolResult(
                    ok=False,
                    summary=f"{inp.cli} edited but verification failed; rolled back",
                    detail=result,
                )

        # keep the change: drop the stash snapshot so it isn't later restored
        if snapshot:
            await self._git(work, "stash", "drop", snapshot)
        changed = bool(diff and diff.strip() and "changed" in diff or "|" in diff)
        return ToolResult(
            ok=True,
            summary=f"{inp.cli} completed in {work}; diff captured",
            detail=result,
        )

    def _build_argv(self, binary: str, inp: DelegateCodingInput) -> list[str]:
        if binary == "opencode":
            return ["opencode", "run", "--dir", inp.working_dir, inp.prompt]
        if binary == "claude":
            args = ["claude", "-p", inp.prompt]
            if inp.skip_permissions:
                args.append("--dangerously-skip-permissions")
            return args
        # pi: non-interactive print mode
        args = ["pi", "-p", "--approve", inp.prompt]
        return args

    async def _run(self, argv: list[str], cwd: Path, timeout: float):
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode

    async def _git(self, cwd: Path, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(cwd), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            return out.decode(errors="replace")
        except (asyncio.TimeoutError, OSError):
            return ""

    async def _restore(self, cwd: Path, snapshot: str, run_id: int | None, why: str) -> None:
        db.audit(run_id, "rollback", f"restoring snapshot ({why})", allowed=True)
        if snapshot:
            await self._git(cwd, "checkout", "--force", snapshot, "--")
        else:
            await self._git(cwd, "checkout", "--force", "HEAD", "--")
        await self._git(cwd, "clean", "-fd")

    async def _verify(self, command: str, cwd: Path) -> tuple[bool, str]:
        decision = self.g.check_shell(command)
        if not decision.allowed:
            return False, f"verify command rejected: {decision.reason}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *decision.argv, cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            return proc.returncode == 0, out.decode(errors="replace")
        except (asyncio.TimeoutError, OSError) as e:
            return False, f"{type(e).__name__}: {e}"
