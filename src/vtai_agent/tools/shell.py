import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..guardrails import GuardrailViolation, Guardrails
from .registry import Tool, ToolResult


def _child_env() -> dict[str, str]:
    """Minimal environment: children never inherit API keys or tokens."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home()),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    keep = ("LC_ALL", "TZ", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    for k in keep:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


class ShellInput(BaseModel):
    command: str = Field(
        description=(
            "Shell command. The binary must be a bare allowlisted name with a "
            "sandbox policy; every path argument is deny/writable-checked."
        ),
    )
    dry_run: bool | None = Field(
        default=None,
        description="Preview without executing. Defaults to config guardrails.dry_run_default.",
    )
    timeout: float = Field(default=30.0, ge=1, le=300)


class ShellTool(Tool[ShellInput]):
    name = "run_shell"
    description = (
        "Run an allowlisted shell command. Denied binaries, protected or "
        "out-of-sandbox path arguments, and dangerous flags raise instead of "
        "executing. Set dry_run=true to preview the plan only."
    )
    InputModel = ShellInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()

    async def run(self, inp: ShellInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        decision = self.g.check_shell(inp.command)
        if not decision.allowed:
            raise GuardrailViolation(decision.reason)

        if dry_run:
            db.audit(run_id, "shell", f"[dry-run] {inp.command}", allowed=True)
            return ToolResult(
                ok=True,
                summary=f"DRY RUN (would execute): {inp.command}",
                detail={"command": inp.command, "argv": decision.argv, "dry_run": True},
                dry_run=True,
            )

        self.g.ensure_not_killed()
        db.audit(run_id, "shell", inp.command, allowed=True)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *decision.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=inp.timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.communicate()
            raise TimeoutError(f"command timed out after {inp.timeout}s: {inp.command}")

        ok = proc.returncode == 0
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        return ToolResult(
            ok=ok,
            summary=f"exit={proc.returncode}: {inp.command}",
            detail={
                "command": inp.command,
                "returncode": proc.returncode,
                "stdout": out[-4000:],
                "stderr": err[-2000:],
            },
        )
