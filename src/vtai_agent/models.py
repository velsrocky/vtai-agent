from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Run(SQLModel, table=True):
    """A single execution of a goal (scheduled, MCP-invoked, or manual)."""

    id: int | None = Field(default=None, primary_key=True)
    goal: str
    status: str = Field(default="pending")  # pending|running|ok|failed|aborted|dry_run
    dry_run: bool = True
    provider: str = ""
    steps_total: int = 0
    steps_done: int = 0
    cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class Step(SQLModel, table=True):
    """One tool call inside a run. Snapshots let us replay any automation."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    idx: int = 0
    tool: str
    input_json: str = ""
    output_json: str = ""
    status: str = Field(default="pending")  # pending|ok|failed|skipped
    created_at: datetime = Field(default_factory=_now)


class AuditLog(SQLModel, table=True):
    """Every guarded action, allowed or denied. Non-repudiable trail."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int | None = Field(default=None, index=True)
    action: str
    detail: str = ""
    allowed: bool = True
    created_at: datetime = Field(default_factory=_now)
