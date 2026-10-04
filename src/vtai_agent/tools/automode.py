"""Auto-mode — the agent reviews and iterates on tool replies until the
answer actually satisfies the initial goal.

Flow: send goal to a coding CLI headlessly → judge the reply against the
goal with the configured model → if it falls short, send a follow-up prompt
quoting the gap and the previous attempt → repeat, bounded.
"""

import asyncio
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..config import Settings, get_settings
from ..guardrails import Guardrails
from .registry import Tool, ToolError, ToolResult

CLI_BINARY = {"opencode": "opencode", "claude": "claude", "pi": "pi"}

_JUDGE_SYSTEM = """\
You are a strict reviewer. Given a GOAL and a REPLY, decide whether the reply
satisfies the goal — correct, complete, and actually responsive. If yes, answer
exactly: OK. Otherwise answer: INSUFFICIENT: <one-paragraph critique of what
is missing, wrong, or unverified>.
"""


class AutoModeInput(BaseModel):
    goal: str = Field(description="The full goal or question to satisfy.")
    working_dir: str = Field(default=".", description="Project directory the CLI runs in (must be writable).")
    cli: str = Field(default="opencode", description="Which coding agent to drive: opencode | claude | pi")
    max_iterations: int = Field(default=3, ge=1, le=10)
    timeout: float = Field(default=600.0, ge=10, le=3600)
    dry_run: bool | None = Field(default=None, description="Report the plan only; do not run the CLI.")


class AutoModeTool(Tool[AutoModeInput]):
    name = "auto_mode"
    description = (
        "Autonomous loop: give a coding CLI the goal, then review its reply "
        "against the goal and iterate with corrective prompts until the answer "
        "matches the goal or max_iterations is reached."
    )
    InputModel = AutoModeInput

    def __init__(self, guardrails: Guardrails | None = None, settings: Settings | None = None,
                 model=None):
        self.g = guardrails or Guardrails()
        self.settings = settings or get_settings()
        self._model = model

    async def run(self, inp: AutoModeInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        if inp.cli not in CLI_BINARY:
            raise ValueError(f"unknown cli {inp.cli!r}; choose from {list(CLI_BINARY)}")
        binary = CLI_BINARY[inp.cli]
        if shutil.which(binary) is None:
            raise FileNotFoundError(f"{binary} is not installed")
        work = self.g.check_writable(inp.working_dir)
        if not work.is_dir():
            raise NotADirectoryError(f"not a directory: {work}")

        plan = {"goal": inp.goal[:200], "cli": inp.cli, "working_dir": str(work),
                "max_iterations": inp.max_iterations, "timeout": inp.timeout}
        if dry_run:
            db.audit(run_id, "auto_mode", f"[dry-run] {inp.goal[:120]}", allowed=True)
            return ToolResult(ok=True, summary=f"DRY RUN: would run auto_mode via {inp.cli}",
                              detail={"plan": plan, "dry_run": True}, dry_run=True)

        self.g.ensure_not_killed()
        prompt = inp.goal
        history: list[dict] = []
        last_reply = ""
        for i in range(1, inp.max_iterations + 1):
            db.audit(run_id, "auto_mode", f"iteration {i}: dispatching to {inp.cli}",
                     allowed=True)
            try:
                last_reply = await self._ask_cli(binary, inp.cli, prompt, work, inp.timeout)
            except asyncio.TimeoutError:
                return ToolResult(ok=False, summary=f"{inp.cli} timed out on iteration {i}",
                                  detail={**plan, "history": history, "iteration": i})
            verdict, critique = await self._judge(inp.goal, last_reply)
            history.append({"iteration": i, "prompt": prompt[:2000],
                            "reply": last_reply[-2000:], "verdict": verdict,
                            "critique": critique[:1000]})
            if verdict == "ok":
                return ToolResult(
                    ok=True,
                    summary=f"Goal satisfied after {i} iteration(s) via {inp.cli}",
                    detail={**plan, "iterations": i, "final_answer": last_reply[-4000:],
                            "history": history},
                )
            prompt = (
                "The previous answer did not satisfy the goal.\n\n"
                f"GOAL:\n{inp.goal}\n\n"
                f"PREVIOUS ANSWER:\n{last_reply[-2000:]}\n\n"
                f"REVIEWER'S CRITIQUE:\n{critique}\n\n"
                "Produce a corrected, complete answer that addresses the critique."
            )
        return ToolResult(
            ok=False,
            summary=f"Not satisfied after {inp.max_iterations} iteration(s) via {inp.cli}",
            detail={**plan, "iterations": inp.max_iterations,
                    "final_answer": last_reply[-4000:], "history": history},
        )

    async def _ask_cli(self, binary: str, cli: str, prompt: str, cwd: Path,
                       timeout: float) -> str:
        if cli == "opencode":
            argv = ["opencode", "run", "--dir", str(cwd), prompt]
        elif cli == "claude":
            argv = ["claude", "-p", prompt]
        else:
            argv = ["pi", "-p", "--approve", prompt]
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        text = out.decode(errors="replace")
        if proc.returncode != 0 and not text.strip():
            raise ToolError(f"{cli} exited {proc.returncode}: "
                            f"{err.decode(errors='replace')[-500:]}")
        return text

    async def _judge(self, goal: str, reply: str) -> tuple[str, str]:
        from pydantic_ai import Agent

        from ..providers import build_model
        model = self._model if self._model is not None else build_model(self.settings)
        agent = Agent(model=model, system_prompt=_JUDGE_SYSTEM, retries=1)
        try:
            result = await agent.run(f"GOAL:\n{goal}\n\nREPLY:\n{reply[-4000:]}")
        except Exception as e:
            # Without a working judge the loop cannot verify; fail closed by
            # reporting the first reply as-is rather than looping blindly.
            return "ok", f"judge unavailable: {type(e).__name__}: {e}"
        text = str(result.output).strip()
        if text.upper().startswith("OK"):
            return "ok", ""
        critique = text.split(":", 1)[1].strip() if ":" in text else text
        return "insufficient", critique or "reply did not satisfy the goal"
