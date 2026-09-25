from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from .config import get_settings
from .models import AuditLog, Run, Step

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


def init_db() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())


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
        return list(s.exec(select(Run).order_by(Run.id.desc()).limit(limit)))
