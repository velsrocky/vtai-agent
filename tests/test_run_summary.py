"""Run telemetry summary (`vtai runs --summary`): aggregation math must be
right on a seeded history — status split, spend percentiles, tool counts,
and denial-code extraction including the error taxonomy."""

from vtai_agent import db
from vtai_agent.errors import CODES


def _seed_history(sandbox):
    """A controlled, known-shaped history: 4 runs, 3 steps, 4 audit rows."""
    r1 = db.start_run("goal one", dry_run=False, provider="local")
    db.log_step(r1, 0, "auto_mode", "{}", "{}", "ok")
    db.finish_run(r1, "ok", cost_usd=0.01)

    r2 = db.start_run("goal two", dry_run=True, provider="local")
    db.log_step(r2, 0, "run_shell", "{}", "{}", "ok")
    db.finish_run(r2, "ok", cost_usd=0.007)

    r3 = db.start_run("goal three", dry_run=False, provider="local")
    db.log_step(r3, 0, "delegate_coding", "{}", "{}", "failed")
    db.finish_run(r3, "failed", cost_usd=0.02)

    r4 = db.start_run("goal four", dry_run=True, provider="local")
    db.finish_run(r4, "aborted", cost_usd=0.0)

    db.audit(None, "shell", "denied binary [code=E_SHELL_BIN_UNTRUSTED]",
             allowed=False)
    db.audit(None, "shell", "denied again [code=E_SHELL_BIN_UNTRUSTED]",
             allowed=False)
    db.audit(None, "write", "protected path [code=E_PATH_PROTECTED]",
             allowed=False)
    db.audit(None, "shell", "allowed attempt", allowed=True)
    return [r1, r2, r3, r4]


def test_summary_aggregates_seeded_history(sandbox):
    _seed_history(sandbox)
    s = db.summarize_runs(limit=50)

    assert s["window"]["runs"] == 4
    assert s["status_counts"] == {"ok": 2, "failed": 1, "aborted": 1}
    assert s["dry_run_share_pct"] == 50

    assert s["spend"]["measured_runs"] == 3
    assert s["spend"]["total_usd"] == 0.037
    assert s["spend"]["mean_usd"] == round(0.037 / 3, 6)
    # sorted [0.007, 0.01, 0.02]: p50 = 0.01, p95 interpolates to 0.019
    assert s["spend"]["p50_usd"] == 0.01
    assert s["spend"]["p95_usd"] == round(0.019, 6)

    assert s["duration_s"]["completed_runs"] == 4  # finish_run stamps even aborted runs
    assert s["duration_s"]["p50"] >= 0.0

    assert s["tools"] == {
        "auto_mode": {"calls": 1, "ok": 1, "failed": 0},
        "run_shell": {"calls": 1, "ok": 1, "failed": 0},
        "delegate_coding": {"calls": 1, "ok": 0, "failed": 1},
    }

    assert s["denials"]["total"] == 3
    assert s["denials"]["by_action"] == {"shell": 2, "write": 1}
    assert s["denials"]["by_code"] == {
        "E_SHELL_BIN_UNTRUSTED": 2, "E_PATH_PROTECTED": 1,
    }


def test_denial_codes_are_real_taxonomy_members(sandbox):
    """Codes counted from the audit trail must exist in errors.CODES — a
    summary metric that invents its own vocabulary is worse than none."""
    _seed_history(sandbox)
    s = db.summarize_runs(limit=50)
    assert s["denials"]["by_code"], "expected at least one counted code"
    for code in s["denials"]["by_code"]:
        assert code in CODES


def test_summary_empty_history(sandbox):
    s = db.summarize_runs()
    assert s == {"window": {"runs": 0}}
    assert db.format_runs_summary(s) == "no runs yet"


def test_format_renders_humans_not_dumps(sandbox):
    _seed_history(sandbox)
    out = db.format_runs_summary(db.summarize_runs(limit=50))
    assert "runs summary — 4 runs" in out
    assert "spend: $0.0370 across 3 measured" in out
    assert "E_SHELL_BIN_UNTRUSTED 2" in out
    assert "denials: none in window" not in out


def test_summary_respects_limit_window(sandbox):
    _seed_history(sandbox)  # 4 runs; limit=2 keeps only the newest two (failed, aborted)
    s = db.summarize_runs(limit=2)
    assert s["window"]["runs"] == 2
    assert s["status_counts"] == {"failed": 1, "aborted": 1}
    # spend/denials only see the truncated window
    assert s["spend"]["measured_runs"] == 1  # only the failed run had cost
    assert s["spend"]["total_usd"] == 0.02
