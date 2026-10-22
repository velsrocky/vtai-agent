"""Auto-mode — the agent reviews and iterates on tool replies until the
answer actually satisfies the initial goal.

Flow: send goal to a coding CLI headlessly → judge the reply against the
goal with the configured model → if it falls short, send a follow-up prompt
quoting the gap and the previous attempt → repeat, bounded.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..config import Settings, get_settings
from ..guardrails import Guardrails
from .registry import Tool, ToolError, ToolResult, resolve_cli_argv

CLI_BINARY = {"opencode": "opencode", "claude": "claude", "pi": "pi"}

_JUDGE_SYSTEM = """\
You are a strict reviewer. Given a GOAL and a REPLY, decide whether the reply
satisfies the goal — correct, complete, and actually responsive. If yes, answer
exactly: OK. Otherwise answer: INSUFFICIENT: <one-paragraph critique of what
is missing, wrong, or unverified>.
"""

# Judge call policy: local models hang or flake, so bound each attempt and
# retry once before failing closed. Both are deliberately small: an
# unverifiable reply must stop the loop fast, not burn max_iterations.
# Judge spend is metered against guardrails.budget_usd_per_run the same way
# orchestrated steps are — combined with the delegated CLI's reported cost
# (claude reports total_cost_usd; see _CLI_COST_CAPABLE).
_JUDGE_ATTEMPTS = 2
_JUDGE_TIMEOUT = 120.0

# Coding CLIs that can report their own headless spend. Verified per vendor
# docs 2026-10-19:
#   claude:  `claude -p --output-format json` -> total_cost_usd
#   opencode/pi: no cost field in headless output — their spend is bounded
#     by max_iterations/timeout and labeled "unmeasured" in run details.
_CLI_COST_CAPABLE = frozenset({"claude"})


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
        budget = self.settings.guardrails.budget_usd_per_run
        cli_spent = 0.0
        for i in range(1, inp.max_iterations + 1):
            db.audit(run_id, "auto_mode", f"iteration {i}: dispatching to {inp.cli}",
                     allowed=True)
            try:
                last_reply, cli_cost = await self._ask_cli(
                    binary, inp.cli, prompt, work, inp.timeout)
            except asyncio.TimeoutError:
                return ToolResult(ok=False, summary=f"{inp.cli} timed out on iteration {i}",
                                  detail={**plan, "history": history, "iteration": i})
            # Spend attribution: a CLI that can report its own spend but
            # yields no readable report must not be treated as free.
            if inp.cli in _CLI_COST_CAPABLE and cli_cost is None:
                return ToolResult(
                    ok=False,
                    summary=(
                        f"cannot verify {inp.cli} spend: its headless output did "
                        "not include total_cost_usd (update the CLI, or use a "
                        "CLI that reports its spend: opencode/pi, or set a local "
                        "provider). Refusing an unverifiable paid spend."),
                    detail={**plan, "history": history, "iteration": i,
                            "spend": {"cli_usd": None, "budget": budget}},
                )
            cli_spent += cli_cost or 0.0
            if cli_spent > budget:
                return ToolResult(
                    ok=False,
                    summary=(f"auto_mode budget (${budget:.2f}) exceeded: {inp.cli} "
                             f"has already spent ${cli_spent:.4f} by iteration {i}."),
                    detail={**plan, "history": history, "iteration": i,
                            "spend": {"cli_usd": cli_spent, "budget": budget}},
                )
            verdict, critique = await self._judge(
                inp.goal, last_reply, budget_remaining=budget - cli_spent)
            # The judge itself is down: no reply can be verified, so stop
            # instead of looping blindly or declaring success.
            if verdict == "error":
                return ToolResult(
                    ok=False,
                    summary=f"cannot verify goal: {critique}",
                    detail={**plan, "iterations": i, "history": history,
                            "final_answer": last_reply[-4000:],
                            "spend": {"cli_usd": cli_spent, "budget": budget}},
                )
            history.append({"iteration": i, "prompt": prompt[:2000],
                            "reply": last_reply[-2000:], "verdict": verdict,
                            "critique": critique[:1000],
                            "cli_cost_usd": cli_cost})
            if verdict == "ok":
                return ToolResult(
                    ok=True,
                    summary=f"Goal satisfied after {i} iteration(s) via {inp.cli}",
                    detail={**plan, "iterations": i, "final_answer": last_reply[-4000:],
                            "history": history,
                            "spend": {"cli_usd": cli_spent if cli_cost is not None
                                      else "unmeasured", "budget": budget}},
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
                    "final_answer": last_reply[-4000:], "history": history,
                    "spend": {"cli_usd": cli_spent, "budget": budget}},
        )

    @staticmethod
    def _cli_argv(cli: str, prompt: str) -> list[str]:
        if cli == "opencode":
            # opencode v2 `run` takes the message positionally and operates in
            # cwd (already set below); it has no --dir flag — an unknown flag
            # makes it print help and exit non-zero. --standalone keeps the
            # run off the shared background service: without it the loop
            # attaches to whatever interactive opencode instance owns that
            # process' project root and works in the wrong repository.
            return ["opencode", "run", "--standalone", prompt]
        if cli == "claude":
            # JSON output: the reply is obj["result"] and the spend is
            # obj["total_cost_usd"] — metered against budget_usd_per_run
            # unless the report is unreadable (then auto_mode fails closed).
            return ["claude", "-p", "--output-format", "json", prompt]
        return ["pi", "-p", "--approve", prompt]

    async def _ask_cli(self, binary: str, cli: str, prompt: str, cwd: Path,
                       timeout: float) -> tuple[str, float | None]:
        """Dispatch one headless CLI turn. Returns (reply, reported_cost_usd);
        reported_cost_usd is None for CLIs without a cost report."""
        argv = self._cli_argv(cli, prompt)
        # Spawn the resolved executable, not the bare name: the server
        # process' PATH can differ from the user's shell, and Windows .cmd
        # shims must go through cmd.exe.
        prefix = resolve_cli_argv(cli)
        if prefix is not None:
            argv = [*prefix, *argv[1:]]
        env = dict(os.environ)
        # Same PWD rule as the delegate tool: opencode picks its project
        # from $PWD, so a stale inherited value points it at the wrong repo.
        env["PWD"] = str(cwd)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        text = out.decode(errors="replace")
        err = err.decode(errors="replace")
        if proc.returncode != 0:
            # A non-zero exit means the CLI did not do the work — even when it
            # printed something (e.g. opencode prints help on bad flags).
            # Returning that text as a "reply" would let the judge approve it.
            raise ToolError(f"{cli} exited {proc.returncode}: "
                            f"{(text or err)[-500:]}")
        if cli == "claude":
            try:
                obj = json.loads(text)
                reply = str(obj.get("result", ""))
                raw = obj.get("total_cost_usd")
                cost = float(raw) if isinstance(raw, (int, float)) else None
            except json.JSONDecodeError:
                return "", None
            return reply, cost
        return text, None

    async def _judge(self, goal: str, reply: str,
                     budget_remaining: float | None = None) -> tuple[str, str]:
        from pydantic_ai import Agent

        from ..providers import build_model, estimate_cost
        try:
            model = self._model if self._model is not None else build_model(self.settings)
            agent = Agent(model=model, system_prompt=_JUDGE_SYSTEM, retries=1)
        except Exception as e:
            # Misconfigured/unavailable provider: no reply can be verified.
            return "error", f"judge unavailable: {type(e).__name__}: {e}"
        budget = (self.settings.guardrails.budget_usd_per_run
                  if budget_remaining is None else budget_remaining)
        spent = 0.0
        last_error: Exception | None = None
        for _ in range(_JUDGE_ATTEMPTS):
            try:
                result = await asyncio.wait_for(
                    agent.run(f"GOAL:\n{goal}\n\nREPLY:\n{reply[-4000:]}"),
                    timeout=_JUDGE_TIMEOUT,
                )
            except Exception as e:
                # Transient inference failure or hung local model: retry
                # before giving up on verification.
                last_error = e
                continue
            # Same fail-closed policy as the orchestrator: a paid judge step
            # whose spend cannot be verified is never treated as free.
            usage = getattr(result, "usage", None)
            est = estimate_cost(self.settings, usage) if usage is not None else None
            if est is None and self.settings.active_provider != "local":
                return "error", (
                    f"cannot verify judge spend for provider "
                    f"'{self.settings.active_provider}'; set cost_per_1k_* "
                    "in config. Refusing to review on an unverifiable spend.")
            spent += est or 0.0
            if spent > budget:
                return "error", (
                    f"judge budget exceeded: ${spent:.4f} of "
                    f"${budget:.2f} (guardrails.budget_usd_per_run)")
            text = str(result.output).strip()
            # Strict verdict token: only a bare "OK" (trailing period allowed)
            # means satisfied. "OKay, mostly..." or "OK, but ..." must count
            # as insufficient — a prefix match would let hedge replies pass
            # an unattended review.
            if text.upper().rstrip(".") == "OK":
                return "ok", ""
            critique = text.split(":", 1)[1].strip() if ":" in text else text
            return "insufficient", critique or "reply did not satisfy the goal"
        assert last_error is not None
        return "error", f"judge unavailable: {type(last_error).__name__}: {last_error}"
