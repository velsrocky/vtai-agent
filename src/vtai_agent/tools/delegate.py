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
from ..guardrails import Guardrails
from .registry import Tool, ToolError, ToolResult

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
        description="Allowlisted command that must pass after the edit (e.g. "
                    "'pytest'). Failure triggers rollback. Relative path args "
                    "are checked against the repo dir.",
    )
    auto_rollback: bool = Field(default=True, description="Restore the git snapshot if verification fails.")
    require_approval: bool = Field(
        default=True,
        description="After the delegate runs and verification passes, show the "
                    "diff and wait for explicit human approval before keeping the "
                    "changes. Non-interactive sessions roll back and report "
                    "'needs_approval' instead. Defaults to True — an unattended "
                    "run cannot silently keep its changes; pass false only when "
                    "a human has already reviewed the task.",
    )
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

        # An unresolved pending approval for this repo is a hard block —
        # including re-runs that flip require_approval. The agent cannot
        # bypass its own approval gate; the human resolves it via `vtai approve`.
        pending = db.pending_approval_for(str(work))
        if pending is not None:
            db.audit(run_id, "approval",
                     f"block: unresolved pending approval #{pending.id} for {work}",
                     allowed=False)
            raise ToolError(
                f"unresolved approval #{pending.id} awaits a human decision for "
                f"{work}; run `vtai approve {pending.id}` to keep the changes or "
                f"`vtai approve {pending.id} --rollback` to discard them first")

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
                detail={"plan": plan, "prompt": inp.prompt, "dry_run": True},
                dry_run=True,
            )

        # Snapshot only when we are actually about to execute: `git add -A` plus
        # a stash ref mutate repo state and must never happen in a preview.
        # A failed snapshot means no rollback guarantee, so we refuse to run.
        # Keep the user's original index tree so it can be restored on exit —
        # the staging we do for snapshot/diff makes everything look staged.
        _, orig_index = await self._git(work, "write-tree")
        orig_index = orig_index.strip()
        rc, _ = await self._git(work, "add", "-A")
        snapshot = ""
        if rc == 0:
            rc, created = await self._git(work, "stash", "create")
            if rc == 0:
                snapshot = (created or "").strip()
        # rc 0 with empty output means the repo was already clean; HEAD is the
        # restore point. Anything else: cannot guarantee rollback.
        if rc != 0:
            await self._restore_index(work, orig_index)
            raise ToolError("cannot snapshot repo (git add/stash failed); refusing to delegate")
        self.g.ensure_not_killed()

        argv = self._build_argv(binary, inp)
        db.audit(run_id, "delegate", f"{inp.cli}: {inp.prompt[:120]}", allowed=True)
        try:
            stdout, stderr, code = await self._run(argv, work, inp.timeout)
        except asyncio.TimeoutError:
            await self._restore(work, snapshot, run_id, "timeout", orig_index)
            return ToolResult(ok=False, summary=f"{inp.cli} timed out after {inp.timeout}s; rolled back",
                              detail={**plan, "rolled_back": True, "reason": "timeout"})

        # claude's JSON mode includes the spend; parse it now that it exists
        # and attach everything to the result dict once it is built below.
        cli_cost: float | None = None
        claude_reply = ""
        claude_cost_note = ""
        if inp.cli == "claude":
            try:
                obj = json.loads(stdout)
                raw = obj.get("total_cost_usd")
                if isinstance(raw, (int, float)):
                    cli_cost = float(raw)
                claude_reply = str(obj.get("result", ""))[:4000]
            except json.JSONDecodeError:
                claude_cost_note = "unparseable headless output"

        _, status = await self._git(work, "status", "--porcelain")
        _, diff = await self._git(work, "diff", "--stat")
        # `git diff` never shows untracked files — and a delegate's newest
        # files are untracked, so list them explicitly, or an add-only change
        # looks like "no changes".
        untracked = [ln[3:].strip() for ln in status.splitlines()
                     if ln.startswith("??")]
        if untracked:
            diff += "\n\nUntracked new files:\n" + "\n".join(untracked)
        result = {
            **plan, "returncode": code, "diff": diff[-4000:],
            "stdout": stdout[-4000:], "stderr": stderr[-2000:],
            # spend attribution: claude reports its own total_cost_usd;
            # opencode/pi have no headless cost field — bounded by timeout.
            "cli_cost_usd": cli_cost if cli_cost is not None else "unmeasured",
        }
        if inp.cli == "claude":
            result["claude_reply"] = claude_reply
        if claude_cost_note:
            result["claude_cost_report"] = claude_cost_note
        db.audit(run_id, "delegate",
                 f"{inp.cli} spend: "
                 f"{'unmeasured' if cli_cost is None else f'${cli_cost:.4f}'}",
                 allowed=True)
        # A delegate that "succeeds" but produced no changes is not a success:
        # pi emits a raw tool-call JSON when it has no authenticated provider,
        # and an unchanged tree can also mean the CLI silently did nothing.
        # Fail closed either way so callers do not proceed on a phantom edit.
        if code == 0 and not diff.strip():
            stripped = stdout.lstrip()
            if stripped.startswith("{") and '"name"' in stripped and '"arguments"' in stripped:
                result.update({
                    "reason": (
                        "delegate proposed a tool call but never executed it "
                        f"(check {binary} auth/session; pi returns this when "
                        "no provider is authenticated)"
                    ),
                    "rolled_back": False,
                })
                return ToolResult(
                    ok=False,
                    summary=f"{inp.cli} did not apply any changes",
                    detail=result,
                )

        if code != 0:
            if inp.auto_rollback:
                await self._restore(work, snapshot, run_id, f"exit {code}", orig_index)
                result["rolled_back"] = True
                result["reason"] = f"delegate exited {code}"
            return ToolResult(ok=False, summary=f"{inp.cli} exited {code}", detail=result)

        # verification gate
        if inp.verify_command:
            v_ok, v_out = await self._verify(inp.verify_command, work)
            result["verify"] = {"ok": v_ok, "output": v_out[-2000:]}
            if not v_ok and inp.auto_rollback:
                await self._restore(work, snapshot, run_id, "verify failed", orig_index)
                result["rolled_back"] = True
                result["reason"] = "verification failed"
                return ToolResult(
                    ok=False,
                    summary=f"{inp.cli} edited but verification failed; rolled back",
                    detail=result,
                )

        # human approval gate: only accept the diff when a human says so
        if inp.require_approval and code == 0:
            import sys
            if sys.stdin.isatty():
                approved, note = await self._approve(diff, work)
                result["approval_note"] = note
                if not approved:
                    await self._restore(work, snapshot, run_id, "human rejected", orig_index)
                    result["rolled_back"] = True
                    result["reason"] = "human did not approve the diff"
                    return ToolResult(
                        ok=False,
                        summary=f"{inp.cli} completed but was not approved; rolled back. "
                                f"Re-run with require_approval=false to keep changes.",
                        detail=result,
                    )
            else:
                # Non-interactive (MCP, scripted, orchestrated): no stdin to
                # prompt. Silently rolling the work back made this gate
                # entirely unusable from agent sessions, so leave the changes
                # in the worktree as a pending approval for a human to resolve
                # via `vtai approve <id> [|--rollback]`. This run reports
                # failure so automated loops stop instead of proceeding.
                pa = db.create_pending_approval(
                    run_id=run_id, cli=inp.cli, prompt=inp.prompt,
                    working_dir=str(work), snapshot=snapshot,
                    orig_index=orig_index, diff=diff)
                db.audit(run_id, "approval",
                         f"pending #{pa.id}: awaiting human decision "
                         f"(changes left in {work})", allowed=False)
                result["needs_approval"] = True
                result["approval_id"] = pa.id
                return ToolResult(
                    ok=False,
                    summary=(
                        f"{inp.cli} completed; changes kept for review as "
                        f"pending approval #{pa.id}. Resolve with "
                        f"`vtai approve {pa.id}` (keep) or "
                        f"`vtai approve {pa.id} --rollback`. Do not re-run "
                        "delegate_coding in this directory until resolved."),
                    detail=result,
                )

        # `git stash create` never adds to the stash list, so there is nothing
        # to drop; the snapshot ref simply becomes unreachable and is pruned.
        # Hand the user back their original staging state.
        await self._restore_index(work, orig_index)
        return ToolResult(
            ok=True,
            summary=f"{inp.cli} completed in {work}; diff captured",
            detail=result,
        )

    async def resolve_pending(self, approval_id: int, keep: bool) -> ToolResult:
        """Human resolution of a pending approval: keep the delegate's
        changes (index handed back to its pre-run staging state) or roll
        the repo back to the pre-run snapshot."""
        pa = db.get_pending_approval(approval_id)
        if pa is None or pa.resolved:
            return ToolResult(
                ok=False,
                summary=f"pending approval #{approval_id} not found or already resolved",
                detail={"approval_id": approval_id},
            )
        work = Path(pa.working_dir)
        if keep:
            await self._restore_index(work, pa.orig_index)
            db.set_pending_resolved(approval_id, keep=True)
            db.audit(pa.run_id, "approval",
                     f"pending #{approval_id}: kept by human", allowed=True)
            return ToolResult(
                ok=True,
                summary=f"#{approval_id}: changes kept in {work}",
                detail={"approval_id": approval_id, "working_dir": pa.working_dir,
                        "kept": True, "diff": pa.diff[-2000:]},
            )
        await self._restore(work, pa.snapshot, pa.run_id,
                            f"approval #{approval_id} rollback", pa.orig_index)
        db.set_pending_resolved(approval_id, keep=False)
        db.audit(pa.run_id, "approval",
                 f"pending #{approval_id}: rolled back by human", allowed=True)
        return ToolResult(
            ok=True,
            summary=f"#{approval_id}: rolled back {work}",
            detail={"approval_id": approval_id, "working_dir": pa.working_dir,
                    "kept": False},
        )

    async def _approve(self, diff_stat: str, work: Path) -> tuple[bool, str]:
        import sys

        if not sys.stdin.isatty():
            db.audit(None, "delegate", "approval required but stdin is not a TTY; rolling back",
                     allowed=False)
            return False, "non-interactive: no approval possible"
        print(f"\n--- {work} diff (--stat) ---\n{diff_stat[:2000]}\n", flush=True)
        try:
            ans = input("Keep these changes? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False, "no answer"
        return (ans in {"y", "yes"}), f"human answered {ans!r}"


    def _build_argv(self, binary: str, inp: DelegateCodingInput) -> list[str]:
        if binary == "opencode":
            # opencode v2 `run` takes the message positionally and operates in
            # cwd (set by _run); it has no --dir flag.
            return ["opencode", "run", inp.prompt]
        if binary == "claude":
            # --output-format json exposes the reply in obj["result"] and the
            # spend in obj["total_cost_usd"] (captured into cli_cost_usd).
            args = ["claude", "-p", "--output-format", "json", inp.prompt]
            if inp.skip_permissions:
                args.append("--dangerously-skip-permissions")
            return args
        # pi: non-interactive print mode
        return ["pi", "-p", "--approve", inp.prompt]

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

    async def _git(self, cwd: Path, *args: str) -> tuple[int, str]:
        """Run git, returning (returncode, combined output). Errors are visible,
        not swallowed into an empty string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(cwd), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            return proc.returncode or 0, out.decode(errors="replace")
        except (asyncio.TimeoutError, OSError) as e:
            return 124, f"{type(e).__name__}: {e}"

    async def _restore_index(self, cwd: Path, orig_index: str) -> None:
        """Put the user's index back the way it was before we staged for the
        snapshot. The worktree is untouched; only staging state is restored."""
        if orig_index:
            await self._git(cwd, "read-tree", orig_index)

    async def _restore(self, cwd: Path, snapshot: str, run_id: int | None, why: str,
                       orig_index: str = "") -> None:
        db.audit(run_id, "rollback", f"restoring snapshot ({why})", allowed=True)
        target = snapshot or "HEAD"
        rc, out = await self._git(cwd, "reset", "--hard", target)
        if rc != 0:
            db.audit(run_id, "rollback", f"git reset --hard {target} failed: {out[-300:]}",
                     allowed=False)
            return
        # A snapshot exists only when the pre-run index included every untracked
        # file (we `git add -A` first), so a clean run means the repo was
        # pristine beforehand and clean -fd removes exactly the delegate's
        # additions.
        await self._git(cwd, "clean", "-fd")
        _, status = await self._git(cwd, "status", "--porcelain")
        if status.strip():
            db.audit(run_id, "rollback",
                     f"rollback incomplete, still dirty:\n{status[:500]}", allowed=False)
        await self._restore_index(cwd, orig_index)

    async def _verify(self, command: str, cwd: Path) -> tuple[bool, str]:
        decision = self.g.check_shell(command, base_dir=cwd)
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
