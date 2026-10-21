from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from .config import get_settings
from .models import AuditLog, PendingApproval, Run, Step

_engine = None


def sweep_orphaned_runs() -> int:
    """Runs still marked 'running' belong to dead processes — close them out
    so history reflects reality and nothing replays a zombie run."""
    from datetime import datetime, timezone

    swept = 0
    with session_scope() as s:
        orphans = list(s.exec(select(Run).where(Run.status == "running")).all())
        for run in orphans:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            s.add(run)
            s.add(AuditLog(run_id=run.id, action="abort",
                           detail="startup sweep: orphaned run marked failed",
                           allowed=True))
            swept += 1
    return swept


def db_path() -> Path:
    return get_settings().data_dir / "vtai.db"


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{db_path()}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


SCHEMA_VERSION = 2


def _current_schema_version() -> int | None:
    """None = fresh DB (no metadata table yet)."""
    import sqlalchemy

    with get_engine().connect() as conn:
        try:
            row = conn.execute(sqlalchemy.text(
                "SELECT version FROM vtai_metadata LIMIT 1"
            )).first()
        except sqlalchemy.exc.OperationalError:
            return None
    return row[0] if row else None


def _set_schema_version(v: int) -> None:
    import sqlalchemy

    with get_engine().begin() as conn:
        conn.execute(sqlalchemy.text(
            "CREATE TABLE IF NOT EXISTS vtai_metadata (version INTEGER)"
        ))
        conn.execute(sqlalchemy.text("DELETE FROM vtai_metadata"))
        conn.execute(sqlalchemy.text(
            "INSERT INTO vtai_metadata (version) VALUES (:v)"
        ), {"v": v})


def init_db() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    version = _current_schema_version()
    if version is not None and version > SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema version {version} is newer than this binary "
            f"({SCHEMA_VERSION}); upgrade vtai-agent"
        )
    SQLModel.metadata.create_all(get_engine())
    if version is None or version < SCHEMA_VERSION:
        # v1 -> v2 added the PendingApproval table, which create_all above
        # produces on an existing DB. Future real migrations go here.
        _set_schema_version(SCHEMA_VERSION)


@contextmanager
def session_scope() -> Iterator[Session]:
    init_db()
    with Session(get_engine()) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def start_run(goal: str, dry_run: bool, provider: str) -> int:
    with session_scope() as s:
        run = Run(goal=goal, dry_run=dry_run, provider=provider, status="running")
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id  # captured before the session closes (avoids DetachedInstanceError)


def finish_run(run_id: int, status: str, cost_usd: float = 0.0,
               dry_run: bool | None = None) -> None:
    from datetime import datetime, timezone

    with session_scope() as s:
        run = s.get(Run, run_id)
        if run:
            run.status = status
            run.finished_at = datetime.now(timezone.utc)
            run.cost_usd = cost_usd
            if dry_run is not None:
                run.dry_run = dry_run
            steps = s.exec(select(Step).where(Step.run_id == run_id)).all()
            run.steps_done = len(steps)
            run.steps_total = len(steps)
            s.add(run)


def log_step(run_id: int, idx: int, tool: str, input_json: str, output_json: str, status: str) -> None:
    with session_scope() as s:
        s.add(Step(
            run_id=run_id, idx=idx, tool=tool,
            input_json=input_json, output_json=output_json, status=status,
        ))


def audit(run_id: int | None, action: str, detail: str, allowed: bool = True) -> None:
    with session_scope() as s:
        s.add(AuditLog(run_id=run_id, action=action, detail=detail, allowed=allowed))


def list_runs(limit: int = 20) -> list[Run]:
    with session_scope() as s:
        runs = list(s.exec(select(Run).order_by(Run.id.desc()).limit(limit)))
        for r in runs:
            s.expunge(r)  # detach safely: attributes already loaded
        return runs


def get_run(run_id: int) -> Run | None:
    with session_scope() as s:
        r = s.get(Run, run_id)
        if r is not None:
            s.expunge(r)
        return r


def list_steps(run_id: int) -> list[Step]:
    """The tool calls of one run, in order — the replayable record."""
    with session_scope() as s:
        rows = list(s.exec(
            select(Step).where(Step.run_id == run_id).order_by(Step.id)
        ).all())
        for r in rows:
            s.expunge(r)
        return rows


def audit_stats() -> tuple[int, int]:
    """(audit rows, database file size in bytes).

    The audit trail is never pruned automatically — it is the record of what
    the agent did — so it is worth *seeing* how large it has become.
    """
    from sqlalchemy import func

    with session_scope() as s:
        rows = s.exec(select(func.count()).select_from(AuditLog)).one()
    path = db_path()
    size = path.stat().st_size if path.exists() else 0
    return int(rows), size


def list_audit_rows(run_id: int, limit: int = 200) -> list[AuditLog]:
    with session_scope() as s:
        rows = list(s.exec(
            select(AuditLog).where(AuditLog.run_id == run_id)
            .order_by(AuditLog.id).limit(limit)
        ).all())
        for r in rows:
            s.expunge(r)
        return rows


def summarize_runs(limit: int = 500) -> dict:
    """Aggregate the last `limit` runs into benchmark-shaped numbers: status
    split, spend distribution, tool-call counts, and — via the error
    taxonomy — which fail-closed codes the guardrails actually fired.

    Evidence over claims: every field is a measurement, not an opinion.
    """
    import json
    import re

    from collections import Counter

    def _percentile(sorted_vals: list[float], q: float) -> float | None:
        if not sorted_vals:
            return None
        idx = q * (len(sorted_vals) - 1)
        lo, hi = int(idx // 1), int(-(-idx // 1))  # floor / ceil
        return sorted_vals[lo] if lo == hi else sorted_vals[lo] + (
            sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)

    with session_scope() as s:
        runs = list(s.exec(select(Run).order_by(Run.id.desc()).limit(limit)))
        if not runs:
            return {"window": {"runs": 0}}
        window_start = min((r.started_at for r in runs if r.started_at), default=None)
        ids = [r.id for r in runs]

        status_counts = Counter(r.status for r in runs)
        dry_share = round(100 * sum(1 for r in runs if r.dry_run) / len(runs))

        costs = sorted((r.cost_usd for r in runs if r.cost_usd > 0.0))
        durations = sorted(
            (r.finished_at - r.started_at).total_seconds()
            for r in runs
            if r.started_at and r.finished_at and r.finished_at >= r.started_at
        )

        tool_counts: Counter[str] = Counter()
        tool_status: dict[str, Counter[str]] = {}
        steps = s.exec(select(Step).where(Step.run_id.in_(ids))).all()
        for st in steps:
            tool_counts[st.tool] += 1
            tool_status.setdefault(st.tool, Counter())[st.status or "none"] += 1

        # Judge-quality telemetry: auto_mode runs an internal judge loop and
        # records the outcome per iteration ("ok"/"insufficient"/"error") in
        # the step row's output_json (a ToolResult dump). Parsing is
        # best-effort and tolerant: rows without a parseable detail block —
        # e.g. orchestrator truncation at 4000 chars — count as unparsed,
        # never as evidence. Verdicts aggregate per provider.
        provider_of = {r.id: (r.provider or "unknown") for r in runs}
        judge_by_provider: dict[str, dict] = {}
        for st in steps:
            if st.tool != "auto_mode":
                continue
            if st.status == "running" and not st.output_json:
                continue  # mid-call placeholder row, not a judge outcome
            agg = judge_by_provider.setdefault(provider_of.get(st.run_id, "unknown"), {
                "steps": 0, "parsed": 0, "unparsed": 0,
                "verdicts": Counter(), "_iters": [],
            })
            agg["steps"] += 1
            try:
                payload = json.loads(st.output_json)
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if not isinstance(detail, dict):
                    raise ValueError("no detail block")
                hist = detail.get("history")
                verdicts = ([h.get("verdict") for h in hist]
                            if isinstance(hist, list) else [])
                for v in verdicts:
                    if isinstance(v, str):
                        agg["verdicts"][v] += 1
                iters = detail.get("iterations")
                agg["_iters"].append(int(iters) if isinstance(iters, int)
                                     else len(verdicts))
                agg["parsed"] += 1
            except (TypeError, ValueError):
                agg["unparsed"] += 1
        judge: dict[str, dict] = {}
        for prov, agg in judge_by_provider.items():
            iters = agg.pop("_iters")
            judge[prov] = {
                "steps": agg["steps"],
                "parsed": agg["parsed"],
                "unparsed": agg["unparsed"],
                "verdicts": dict(agg["verdicts"]),
                "iterations": {
                    "runs": len(iters),
                    "mean": round(sum(iters) / len(iters), 2) if iters else None,
                    "max": max(iters) if iters else None,
                },
            }

        denials_by_code: Counter[str] = Counter()
        denials_by_action: Counter[str] = Counter()
        if window_start is not None:
            denied = s.exec(
                select(AuditLog).where(
                    AuditLog.allowed == False,  # noqa: E712 — needed by sqlmodel filter
                    AuditLog.created_at >= window_start)
            ).all()
            for a in denied:
                denials_by_action[a.action] += 1
                for code in re.findall(r"\[code=(E_[A-Z_]+)\]", a.detail):
                    denials_by_code[code] += 1

        return {
            "window": {
                "runs": len(runs),
                "oldest": (runs[-1].started_at.isoformat()
                           if runs[-1].started_at else None),
                "newest": (runs[0].started_at.isoformat()
                           if runs[0].started_at else None),
            },
            "status_counts": dict(status_counts),
            "dry_run_share_pct": dry_share,
            "spend": {
                "total_usd": round(sum(costs), 6),
                "measured_runs": len(costs),
                "mean_usd": round(sum(costs) / len(costs), 6) if costs else 0.0,
                "p50_usd": _percentile(costs, 0.5),
                "p95_usd": _percentile(costs, 0.95),
            },
            "duration_s": {
                "completed_runs": len(durations),
                "p50": round(_percentile(durations, 0.5), 1) if durations else None,
                "p95": round(_percentile(durations, 0.95), 1) if durations else None,
            },
            "tools": {
                tool: {
                    "calls": n,
                    "ok": tool_status[tool].get("ok", 0),
                    "failed": tool_status[tool].get("failed", 0),
                }
                for tool, n in tool_counts.most_common()
            },
            "denials": {
                "total": sum(denials_by_action.values()),
                "by_action": dict(denials_by_action.most_common()),
                "by_code": dict(denials_by_code.most_common()),
            },
            "judge": dict(sorted(judge.items(),
                                 key=lambda kv: -kv[1]["steps"])),
        }


def format_runs_summary(summ: dict) -> str:
    """Human one-screen rendering of summarize_runs()."""
    if summ["window"]["runs"] == 0:
        return "no runs yet"
    w = summ["window"]
    lines = [f"runs summary — {w['runs']} runs "
             f"({str(w['oldest'])[:16]} .. {str(w['newest'])[:16]})"]
    sc = summ["status_counts"]
    lines.append("  status: " + ", ".join(f"{k} {v}" for k, v in sc.items())
                 + f" | dry-run-first share {summ['dry_run_share_pct']}%")
    sp = summ["spend"]
    if sp["measured_runs"]:
        lines.append(
            f"  spend: ${sp['total_usd']:.4f} across {sp['measured_runs']} measured"
            f" | mean ${sp['mean_usd']:.4f} | p50 ${sp['p50_usd']:.4f}"
            f" | p95 ${sp['p95_usd']:.4f}")
    else:
        lines.append("  spend: no measured spend (dry-run only or CLI not cost-capable)")
    du = summ["duration_s"]
    if du["completed_runs"]:
        lines.append(f"  duration: {du['completed_runs']} completed"
                     f" | p50 {du['p50']}s | p95 {du['p95']}s")
    if summ["tools"]:
        ranked = list(summ["tools"].items())
        shown = ranked[:8]
        more = len(ranked) - len(shown)
        lines.append("  tools: " + " | ".join(
            f"{t} {d['calls']} (ok {d['ok']})" for t, d in shown)
            + (f" | +{more} more" if more > 0 else ""))
    for prov, jd in list(summ["judge"].items())[:3]:
        parts = [f"  judge[{prov}]: {jd['steps']} calls"]
        if jd["unparsed"]:
            parts.append(f"unparsed {jd['unparsed']} (truncated/unreadable rows)")
        if jd["verdicts"]:
            parts.append("verdicts " + " ".join(
                f"{k} {v}" for k, v in sorted(jd["verdicts"].items())))
        it = jd["iterations"]
        if it["runs"]:
            parts.append(f"iters mean {it['mean']} max {it['max']}")
        lines.append(" | ".join(parts))
    dn = summ["denials"]
    if dn["total"]:
        codes = ", ".join(f"{c} {n}" for c, n in dn["by_code"].items()) or "no codes"
        lines.append(f"  denials: {dn['total']} — {codes}"
                     + (" | by action: " + ", ".join(
                         f"{k} {v}" for k, v in dn["by_action"].items())))
    else:
        lines.append("  denials: none in window")
    return "\n".join(lines)


# ---------- pending approvals (delegate_coding human gate) ----------
def create_pending_approval(run_id: int | None, cli: str, prompt: str,
                            working_dir: str, snapshot: str, orig_index: str,
                            diff: str) -> PendingApproval:
    with session_scope() as s:
        pa = PendingApproval(
            run_id=run_id, cli=cli, prompt=prompt[:2000],
            working_dir=working_dir, snapshot=snapshot, orig_index=orig_index,
            diff=diff[:8000],
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        s.expunge(pa)
        return pa


def get_pending_approval(pa_id: int) -> PendingApproval | None:
    with session_scope() as s:
        pa = s.get(PendingApproval, pa_id)
        if pa is not None:
            s.expunge(pa)
        return pa


def pending_approval_for(working_dir: str) -> PendingApproval | None:
    """The unresolved pending approval for a working dir, if any."""
    with session_scope() as s:
        pa = s.exec(select(PendingApproval)
                    .where(PendingApproval.working_dir == working_dir)
                    .where(PendingApproval.resolved.is_(False))
                    ).first()
        if pa is not None:
            s.expunge(pa)
        return pa


def list_pending_approvals(limit: int = 20) -> list[PendingApproval]:
    with session_scope() as s:
        rows = list(s.exec(
            select(PendingApproval)
            .where(PendingApproval.resolved.is_(False))
            .order_by(PendingApproval.id.desc())
            .limit(limit)
        ))
        for pa in rows:
            s.expunge(pa)
        return rows


def set_pending_resolved(pa_id: int, keep: bool) -> PendingApproval | None:
    from datetime import datetime, timezone

    with session_scope() as s:
        pa = s.get(PendingApproval, pa_id)
        if pa is None:
            return None
        pa.resolved = True
        pa.kept = keep
        pa.resolved_at = datetime.now(timezone.utc)
        s.add(pa)
        s.commit()
        s.refresh(pa)
        s.expunge(pa)
        return pa
