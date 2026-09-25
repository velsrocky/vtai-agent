import asyncio
import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import pytest
from sqlmodel import select

from pydantic_ai.models.test import TestModel

from vtai_agent import db
from vtai_agent.config import (
    Guardrails as GuardrailsConfig,
    ProviderOpenAI,
    Settings,
)
from vtai_agent.mcp_server import _call, register_tools
from vtai_agent.models import AuditLog, Run
from vtai_agent.orchestrator import Orchestrator
from vtai_agent.tools import registry


def _orch_settings(sandbox, budget: float) -> Settings:
    return Settings(
        active_provider="openai",
        openai=ProviderOpenAI(model="test", cost_per_1k_input=1.0, cost_per_1k_output=1.0),
        guardrails=GuardrailsConfig(
            dry_run_default=True,
            max_steps=5,
            max_retries=1,
            budget_usd_per_run=budget,
            deny_globs=sandbox.cfg.deny_globs,
            trusted_bin_dirs=sandbox.cfg.trusted_bin_dirs,
        ),
        paths=sandbox.paths,
    )


def _latest_run() -> dict:
    with db.session_scope() as s:
        run = s.exec(select(Run).order_by(Run.id.desc())).first()
        return run.model_dump() if run else {}


def _audits(run_id: int) -> list[dict]:
    with db.session_scope() as s:
        return [a.model_dump() for a in
                s.exec(select(AuditLog).where(AuditLog.run_id == run_id)).all()]


# ---------- budget enforcement ----------
def test_budget_exceeded_aborts_run(sandbox):
    orch = Orchestrator(settings=_orch_settings(sandbox, 0.0000000001),
                        model=TestModel(custom_output_text="done"))
    res = asyncio.run(orch.run_goal("noop"))
    assert res.status == "budget_exceeded"
    assert res.cost_usd > 0
    assert "budget" in res.final_answer.lower()
    assert _latest_run()["status"] == "budget_exceeded"


def test_within_budget_completes_and_records_cost(sandbox):
    orch = Orchestrator(settings=_orch_settings(sandbox, 10.0),
                        model=TestModel(custom_output_text="all done"))
    res = asyncio.run(orch.run_goal("noop"))
    assert res.status == "ok"
    assert res.final_answer == "all done"
    run = _latest_run()
    assert run["status"] == "ok"
    assert run["cost_usd"] == pytest.approx(res.cost_usd)
    assert run["cost_usd"] > 0


def test_local_provider_without_pricing_is_free(sandbox):
    s = _orch_settings(sandbox, 10.0)
    object.__setattr__(s, "active_provider", "local")
    from vtai_agent.providers import estimate_step_cost

    class U:
        input_tokens = 1000
        output_tokens = 1000
        cost = None

    assert estimate_step_cost(s, U()) == 0.0


# ---------- direct-call attribution ----------
@pytest.fixture
def tools(sandbox):
    registry.clear()
    register_tools()
    yield


def test_direct_call_creates_attributed_run_and_audit(sandbox, tools):
    out = asyncio.run(_call("run_shell",
                            {"command": f"ls {sandbox.writable}", "dry_run": True}))
    assert "DRY RUN" in out
    run = _latest_run()
    assert run["goal"] == "tool:run_shell"
    assert run["provider"] == "mcp"
    assert run["status"] == "ok"
    assert run["dry_run"] is True
    assert len(_audits(run["id"])) >= 1


def test_denied_call_records_denied_status(sandbox, tools):
    out = asyncio.run(_call("run_shell", {"command": "rm -rf /"}))
    assert out.startswith("[GUARDRAIL DENIED]")
    run = _latest_run()
    assert run["status"] == "denied"
    assert any(not a["allowed"] for a in _audits(run["id"]))


def test_failed_tool_records_failed_status(sandbox, tools):
    out = asyncio.run(_call("backup_sync", {
        "source": str(sandbox.tmp / "nonexistent"),
        "destination": str(sandbox.writable / "dst"),
    }))
    assert out.startswith("[ERROR]")
    assert _latest_run()["status"] == "failed"
