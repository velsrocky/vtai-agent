import asyncio
import json

from pydantic import BaseModel, Field

from .. import db
from ..guardrails import GuardrailViolation, Guardrails, get_settings
from .registry import Tool, ToolResult


class ShellInput(BaseModel):
    command: str = Field(description="Shell command. The binary (argv[0]) must be in the allowlist.")
    dry_run: bool | None = Field(
        default=None,
        description="Preview without executing. Defaults to config guardrails.dry_run_default.",
    )
    timeout: float = Field(default=30.0, ge=1, le=300)


class ShellTool(Tool[ShellInput]):
    name = "run_shell"
    description = (
        "Run an allowlisted shell command. Denied binaries and protected paths "
        "raise instead of executing. Set dry_run=true to preview the plan only."
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

        db.audit(run_id, "shell", inp.command, allowed=True)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *decision.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
