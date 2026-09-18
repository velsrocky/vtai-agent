"""Orchestrator — the bounded planner/actor loop.

Uses a text-based ReAct protocol rather than native function calling, so it
works identically with a small local Ollama model and a frontier cloud model.
The model reasons in plain text and emits tool calls as fenced JSON; we parse,
guard, execute via the shared tool registry, and feed results back — until it
produces a final answer or hits a bound.

Bounds enforced every step: max_steps, kill switch, and (later) budget caps.
Every step is persisted, so any run is replayable and auditable.
"""

import json
import re
from dataclasses import dataclass, field

from pydantic_ai import Agent

from . import db
from .config import Settings, get_settings
from .guardrails import GuardrailViolation, Guardrails
from .providers import build_model, describe_active
from .tools import ToolError, registry


_SYSTEM = """\
You are VT-AIAgent, an automation agent operating on this Linux machine.
You work toward the user's goal by calling tools, then reasoning about results.

## Protocol
- To act, reply with ONLY a fenced JSON block and nothing else:
  ```json
  {{"tool": "<name>", "args": {{...}}}}
  ```
- To finish, reply with a short natural-language summary and no JSON block.
- Never narrate a tool call outside the JSON block. One tool per turn.

## Tools
{manifest}

## Rules
- Tool results come back as "TOOL RESULT [...]". Read them before acting again.
- If a tool is denied or fails, do not repeat the same call. Adapt or finish.
- Stay inside writable_roots; writes outside are rejected.
- When done, summarize what changed (or what would change, if dry run).

## Current mode
{mode}
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_RE = re.compile(r'(\{\s*"tool"\s*:.*\})', re.DOTALL)


@dataclass
class ParsedToolCall:
    tool: str
    args: dict


@dataclass
class RunResult:
    run_id: int
    status: str
    steps: int
    final_answer: str
    dry_run: bool
    provider: str
    errors: list[str] = field(default_factory=list)


def build_tool_manifest() -> str:
    lines = []
    for name, tool in registry.all().items():
        schema = tool.InputModel.model_json_schema()
        props = schema.get("properties", {})
        req = schema.get("required", [])
        fields_desc = []
        for fname, f in props.items():
            t = f.get("type", "any")
            desc = f.get("description", "")
            marker = "required" if fname in req else "optional"
            default = f.get("default")
            dstr = f', default={default!r}' if default is not None else ""
            fields_desc.append(f'    "{fname}" ({t}, {marker}{dstr}): {desc}')
        lines.append(
            f'- {name}: {tool.description.splitlines()[0]}\n'
            + "\n".join(fields_desc)
        )
    return "\n".join(lines)


def parse_tool_call(text: str) -> ParsedToolCall | None:
    """Extract a tool call from the model's text, or None if it's a final answer."""
    m = _FENCE_RE.search(text) or _BARE_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "tool" not in obj:
        return None
    return ParsedToolCall(tool=str(obj["tool"]), args=obj.get("args") or {})


class Orchestrator:
    def __init__(self, settings: Settings | None = None, guardrails: Guardrails | None = None):
        self.settings = settings or get_settings()
        self.g = guardrails or Guardrails(self.settings.guardrails)
        self.model = build_model(self.settings)

    async def run_goal(self, goal: str, dry_run: bool | None = None) -> RunResult:
        dry_run = dry_run if dry_run is not None else self.g.dry_run_default
        provider = describe_active(self.settings)["provider"]
        run_id = db.start_run(goal=goal, dry_run=dry_run, provider=provider)

        agent = Agent(
            model=self.model,
            system_prompt=_SYSTEM.format(
                manifest=build_tool_manifest(),
                mode=(
                    "PREVIEW: pass dry_run: true on every call. Change nothing — "
                    "this run only previews what would happen."
                    if dry_run else
                    "EXECUTE: you may pass dry_run: false to make real changes. "
                    "Preview with dry_run: true first if uncertain, then execute."
                ),
            ),
            retries=self.settings.guardrails.max_retries,
        )

        history = []
        prompt = goal
        errors: list[str] = []
        steps = 0
        status = "failed"
        final_answer = ""

        try:
            for step in range(self.settings.guardrails.max_steps):
                if self.g.kill_switch_active():
                    status = "aborted"
                    final_answer = "Kill switch active — run aborted by operator."
                    db.audit(run_id, "abort", "kill switch", allowed=True)
                    break

                result = await agent.run(prompt, message_history=history)
                history = result.all_messages()
                text = result.output.strip()
                steps = step + 1

                call = parse_tool_call(text)
                if call is None:
                    status = "ok"
                    final_answer = text
                    db.log_step(run_id, step, "final_answer", goal, text[:2000], "ok")
                    break

                db.log_step(run_id, step, call.tool, json.dumps(call.args)[:2000], "", "running")
                # The run mode is authoritative — the model may not silently
                # downgrade an EXECUTE run into a preview, nor execute in PREVIEW.
                if dry_run:
                    call.args["dry_run"] = True
                else:
                    call.args.setdefault("dry_run", False)
                try:
                    tr = await registry.call(call.tool, call.args, run_id=run_id)
                    outcome = "ok"
                    payload = tr.model_dump_json()[:4000]
                    if not tr.ok:
                        outcome = "failed"
                        errors.append(f"{call.tool}: {tr.summary}")
                except (GuardrailViolation, ToolError) as e:
                    outcome = "failed"
                    payload = f"ERROR: {e}"
                    errors.append(f"{call.tool}: {e}")
                except Exception as e:
                    outcome = "failed"
                    payload = f"UNEXPECTED: {type(e).__name__}: {e}"
                    errors.append(f"{call.tool}: {e}")

                db.log_step(run_id, step, call.tool, json.dumps(call.args)[:2000], payload, outcome)
                prompt = f"TOOL RESULT [{call.tool}]:\n{payload}"
            else:
                status = "failed"
                final_answer = (
                    f"Reached max_steps ({self.settings.guardrails.max_steps}) "
                    f"without a final answer."
                )
                errors.append("max_steps exhausted")
        finally:
            db.finish_run(run_id, status)

        return RunResult(
            run_id=run_id, status=status, steps=steps,
            final_answer=final_answer, dry_run=dry_run,
            provider=provider, errors=errors,
        )
