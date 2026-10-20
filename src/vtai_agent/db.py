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
