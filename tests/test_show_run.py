"""`vtai show <run_id>` is the answer to "what exactly did that run do?":
the goal, every tool call, what the guardrails allowed or refused, and the
spend — in one place."""

from vtai_agent import db
from vtai_agent.cli import cmd_show
from vtai_agent.models import AuditLog, Run, Step


def _make_run(sandbox) -> int:
    with db.session_scope() as s:
        run = Run(goal="organize my downloads", status="ok", dry_run=False,
                  provider="local", steps_total=2, steps_done=2, cost_usd=0.0123)
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
        s.add(Step(run_id=run_id, idx=0, tool="disk_audit",
                   input_json='{"path": "~/Downloads"}',
                   output_json='{"summary": "12 large files"}',
                   status="ok"))
        s.add(Step(run_id=run_id, idx=1, tool="run_shell",
                   input_json='{"command": "cat ~/.ssh/id_rsa"}',
                   output_json='{"error": "Protected path"}',
                   status="failed"))
        s.add(AuditLog(run_id=run_id, action="run_shell",
                       detail="argument rejected: Protected path: ~/.ssh/id_rsa",
                       allowed=False))
        s.add(AuditLog(run_id=run_id, action="disk_audit",
                       detail="read /Users/you/Downloads", allowed=True))
        s.commit()
    return run_id


def test_show_prints_the_goal_steps_and_audit(capsys, sandbox):
    run_id = _make_run(sandbox)
    rc = cmd_show(type("Args", (), {"run_id": run_id})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "organize my downloads" in out
    assert "disk_audit" in out
    assert "run_shell" in out
    assert "DENY" in out
    assert "1 denied" in out
    assert "$0.0123" in out


def _run_with_step(sandbox, *, output_json: str) -> int:
    with db.session_scope() as s:
        run = Run(goal="panel goal", status="ok", dry_run=False,
                  provider="local", steps_total=1, steps_done=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
        s.add(Step(run_id=run_id, idx=0, tool="auto_mode",
                   input_json='{"goal": "panel goal"}',
                   output_json=output_json, status="ok"))
        s.commit()
    return run_id


def test_show_renders_judge_panel_verdicts(capsys, sandbox):
    """Panel observability: `vtai show` discloses each iteration's replica
    verdicts next to the aggregate, like it already does for tool calls."""
    from vtai_agent.tools.registry import ToolResult
    detail = {"plan": {"max_iterations": 3}, "iterations": 2, "history": [
        {"iteration": 1, "verdict": "insufficient", "critique": "too terse",
         "judge_panel": {"replicas": 3,
                         "verdicts": ["ok", "insufficient", "ok"]}},
        {"iteration": 2, "verdict": "ok", "critique": "",
         "judge_panel": {"replicas": 3, "verdicts": ["ok", "ok", "ok"]}},
    ], "final_answer": "done", "spend": {"cli_usd": 0.0, "judge_usd": 0.0,
                                          "budget": 10.0}}
    run_id = _run_with_step(sandbox, output_json=ToolResult(
        ok=True, summary="satisfied", detail=detail).model_dump_json())
    rc = cmd_show(type("Args", (), {"run_id": run_id})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "judge(3): ok / insufficient / ok -> insufficient" in out
    assert "judge(3): ok / ok / ok -> ok" in out


def test_show_keeps_legacy_auto_mode_rows_unchanged(capsys, sandbox):
    """Rows recorded before judge_panel existed must render exactly as
    before — no invented panels, no stray blank lines."""
    from vtai_agent.tools.registry import ToolResult
    detail = {"plan": {"max_iterations": 2}, "iterations": 1, "history": [
        {"iteration": 1, "verdict": "ok", "critique": "", "cli_cost_usd": 0.0},
    ], "final_answer": "done"}
    run_id = _run_with_step(sandbox, output_json=ToolResult(
        ok=True, summary="satisfied", detail=detail).model_dump_json())
    rc = cmd_show(type("Args", (), {"run_id": run_id})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "judge(" not in out


def test_show_reports_a_missing_run(capsys, sandbox):
    rc = cmd_show(type("Args", (), {"run_id": 4242})())
    err = capsys.readouterr().err
    assert rc == 1
    assert "no run #4242" in err


def test_show_survives_a_run_with_no_steps(capsys, sandbox):
    with db.session_scope() as s:
        run = Run(goal="nothing to do", status="ok")
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    rc = cmd_show(type("Args", (), {"run_id": run_id})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "steps: none recorded" in out
