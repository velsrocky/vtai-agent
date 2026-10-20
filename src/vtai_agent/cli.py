import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, db
from .config import get_settings
from .providers import describe_active
from .tools import registry
from .mcp_server import register_tools

VOICE_HELPERS = ("agent-listen", "agent-speak")


def _voice_src_dir() -> Path | None:
    """Versioned helper sources. $VTAI_SCRIPTS_DIR wins (tests, custom
    checkouts), then the repo-adjacent scripts/ dir. A global `uv tool`
    install ships no scripts/, so sync must run from a checkout."""
    env = os.environ.get("VTAI_SCRIPTS_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    repo = Path(__file__).resolve().parents[2] / "scripts"
    return repo if repo.is_dir() else None


def voice_sync(src: Path, dest: Path, *, check: bool = False) -> tuple[dict[str, str], bool]:
    """Copy voice helpers src/ → dest/. Returns (status per helper, in_sync).

    Status is one of: installed (new file), updated (checksum differed),
    unchanged, missing-source. in_sync is True when dest matches src after
    the call (in check mode: whether it already matched)."""
    states: dict[str, str] = {}
    failed = False
    for name in VOICE_HELPERS:
        s = src / name
        d = dest / name
        if not s.is_file():
            states[name] = "missing-source"
            failed = True
            continue
        want = hashlib.sha256(s.read_bytes()).hexdigest()
        have = hashlib.sha256(d.read_bytes()).hexdigest() if d.is_file() else None
        if have == want:
            states[name] = "unchanged"
            continue
        states[name] = "installed" if have is None else "updated"
        if check:
            failed = True
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            try:
                d.chmod(d.stat().st_mode | 0o111)
            except OSError:
                pass
    return states, not failed


def cmd_voice_sync(args) -> int:
    src = _voice_src_dir()
    if src is None:
        print("voice helper sources not found: run from a checkout or set "
              "VTAI_SCRIPTS_DIR", file=sys.stderr)
        return 1
    dest = Path(args.dest).expanduser()
    states, in_sync = voice_sync(src, dest, check=args.check)
    for name, state in states.items():
        print(f"{name:14} {state}")
    if args.check and not in_sync:
        print(f"helpers out of sync with {src} (run without --check to fix)",
              file=sys.stderr)
        return 1
    if in_sync:
        print(f"helpers in sync ({dest})")
    else:
        print(f"helpers synced to {dest} — ensure it is on PATH")
    return 0


STARTER_BINS = [
    "ls", "cat", "head", "tail", "wc", "du", "df", "free", "grep",
    "file", "stat", "mkdir", "mv", "cp", "rsync", "tar", "zip", "unzip",
    "nproc", "uptime", "pytest",
]
# free/nproc are Linux-only; macOS gets vm_stat instead (policy in
# guardrails.py). Missing binaries deny harmlessly, but a starter config
# should only list binaries that can actually run on the new machine.
STARTER_BINS_LINUX_ONLY = {"free", "nproc"}
STARTER_BINS_MAC_ONLY = ["vm_stat"]


def _starter_allowlist() -> list[str]:
    bins = [b for b in STARTER_BINS if b not in STARTER_BINS_LINUX_ONLY] \
        if sys.platform == "darwin" else list(STARTER_BINS)
    if sys.platform == "darwin":
        bins += STARTER_BINS_MAC_ONLY
    return bins


def _starter_toml() -> str:
    allow = ",\n  ".join(f'"{b}"' for b in _starter_allowlist())
    return f"""\
active_provider = "local"

[local]
base_url = "http://localhost:11434/v1"
model    = "qwen2.5-coder:7b"
api_key  = "ollama"

[guardrails]
dry_run_default  = true
max_steps        = 40
max_retries      = 3
budget_usd_per_run = 1.0
shell_allowlist = [
  {allow},
]
deny_globs = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "**/.env"]

[guardrails.shell_extra_modes]
pytest = "write"

[paths]
writable_roots = ["~/Downloads", "~/Documents/vt-data", "~/.vtaiagent/tmp"]
data_dir = "~/.vtaiagent"
"""


STARTER_TOML = _starter_toml()


def cmd_init(args) -> int:
    """Bootstrap: create config/settings.toml (if missing), writable roots,
    and the data dir, then validate. Safe to re-run."""
    from pathlib import Path

    from .config import CONFIG_TOML, Settings, config_source, validate_settings

    rc = 0
    if CONFIG_TOML.exists() and not args.force:
        print(f"config exists: {CONFIG_TOML} (use --force to regenerate)")
        if config_source() == "repo-adjacent":
            # The checked-in config belongs to the checkout, not the user.
            # Point at the per-user alternative instead of overwriting a
            # tracked file with a starter config.
            print(
                "  note: this is the checkout's own config/settings.toml.\n"
                "        For your own settings, keep it untouched and use:\n"
                "          export VT_CONFIG=\"$HOME/.vtaiagent/settings.toml\"\n"
                "          vtai init        # writes a starter config there\n"
            )
        try:
            validate_settings(Settings())
            print("config validates OK")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            rc = 1
    else:
        CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_TOML.write_text(STARTER_TOML)
        print(f"wrote starter config: {CONFIG_TOML}")

    for d in ["~/.vtaiagent", "~/.vtaiagent/tmp", "~/Documents/vt-data"]:
        p = Path(d).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        print(f"ensured dir: {p}")
    Path("~/Downloads").expanduser().mkdir(parents=True, exist_ok=True)

    try:
        validate_settings(Settings())
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("\nNext: uv run vtai info && uv run vtai goal --dry-run \"list my Downloads\"")
    return rc


def cmd_info(_args) -> int:
    from .config import CONFIG_TOML, config_source

    print(f"VT-AIAgent v{__version__}")
    print(f"  config file     : {CONFIG_TOML} ({config_source()})")
    try:
        s = get_settings()
    except ValueError as e:
        # `info` is the diagnostic command: an invalid config is the thing
        # the user is here to find, so report it instead of raising.
        print("  config          : INVALID")
        print(str(e), file=sys.stderr)
        return 1
    print(f"  active provider : {describe_active(s)}")
    print(f"  data dir        : {s.paths.data_dir}")
    print(f"  writable roots  : {[str(p) for p in s.paths.writable_roots]}")
    print(f"  dry_run_default : {s.guardrails.dry_run_default}")
    print(f"  shell allowlist : {s.guardrails.shell_allowlist}")
    _print_pending_approvals()
    return 0


def _print_pending_approvals() -> None:
    """A delegate that was kept for review is easy to forget — surface the
    count in `vtai info` so an unattended run is visible without knowing to
    ask for `vtai approve`."""
    from . import db

    try:
        rows = db.list_pending_approvals()
    except Exception as e:  # the DB may not exist yet; info must not fail
        print(f"  pending approvals: unknown ({e})")
        return
    if not rows:
        print("  pending approvals: none")
        return
    print(f"  pending approvals: {len(rows)} (vtai approve — keep or --rollback)")
    for pa in rows[:5]:
        print(f"    #{pa.id:<4} {pa.cli:<8} {pa.working_dir}")


def cmd_tools(_args) -> int:
    register_tools()
    for name, tool in registry.all().items():
        print(f"{name:16} {tool.description.splitlines()[0]}")
    return 0


def cmd_run(args) -> int:
    register_tools()
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        print(f"invalid --params JSON: {e}", file=sys.stderr)
        return 2
    try:
        from .mcp_server import tracked_call
        result = asyncio.run(tracked_call(args.tool, params, source="cli"))
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0 if result.ok else 1


def _http_app(host: str, token: str):
    """The HTTP transport app: the MCP server plus bearer auth when a token
    is set. Extracted so the auth path is testable without a live server."""
    from .mcp_server import build_server

    app = build_server().streamable_http_app(host=host)

    if token:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class _BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(_BearerAuth)
    return app


def cmd_serve(args) -> int:
    import os

    from .mcp_server import build_server

    if args.transport == "stdio":
        build_server().run(transport="stdio")
        return 0

    # HTTP transport: expose the same MCP tools over HTTP for app integration.
    token = args.token or os.environ.get("VT_HTTP_TOKEN", "")
    if not token and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind a non-localhost address without "
              "--token or VT_HTTP_TOKEN", file=sys.stderr)
        return 2

    import uvicorn

    app = _http_app(args.host, token)

    try:
        db.sweep_orphaned_runs()
    except Exception:
        pass
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_goal(args) -> int:
    register_tools()
    from .orchestrator import Orchestrator
    orch = Orchestrator()
    dry = None if args.dry_run is None else args.dry_run
    result = asyncio.run(orch.run_goal(args.goal, dry_run=dry))
    print(f"\n[run #{result.run_id}] status={result.status} steps={result.steps} "
          f"dry_run={result.dry_run} provider={result.provider} "
          f"cost=${result.cost_usd:.4f}")
    if result.errors:
        print("errors:")
        for e in result.errors:
            print(f"  - {e}")
    print(f"\n{result.final_answer}")
    return 0 if result.status == "ok" else 1


def cmd_approve(args) -> int:
    """Resolve pending delegate_coding approvals."""
    if args.approval_id is None or args.list:
        rows = db.list_pending_approvals()
        if not rows:
            print("no pending approvals")
            return 0
        for pa in rows:
            created = pa.created_at.strftime("%Y-%m-%d %H:%M") if pa.created_at else "?"
            print(f"#{pa.id:<4} {created} run={pa.run_id} {pa.cli:<8} "
                  f"{pa.working_dir}  prompt: {pa.prompt[:60]}")
        return 0

    from .guardrails import Guardrails
    from .tools.delegate import DelegateCodingTool

    tool = DelegateCodingTool(Guardrails())
    result = asyncio.run(tool.resolve_pending(args.approval_id, keep=not args.rollback))
    print(result.summary)
    if result.detail.get("diff"):
        print(f"\n--- kept diff (tail) ---\n{result.detail['diff']}")
    return 0 if result.ok else 1


def cmd_doctor(_args) -> int:
    """Preflight: is this machine set up to run the agent?"""
    from .doctor import run_doctor

    return run_doctor()


def cmd_audit(args) -> int:
    from sqlmodel import select

    from .models import AuditLog
    with db.session_scope() as s:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(args.limit)
        rows = [r.model_dump() for r in s.exec(q)]
    if args.action:
        rows = [r for r in rows if r["action"] == args.action]
    if args.run_id is not None:
        rows = [r for r in rows if r["run_id"] == args.run_id]
    if args.allowed is not None:
        rows = [r for r in rows if r["allowed"] == args.allowed]
    if args.grep:
        g = args.grep.lower()
        rows = [r for r in rows if g in r["detail"].lower()]
    if not rows:
        print("no audit rows matched")
        return 0
    for r in rows:
        verdict = "allow" if r["allowed"] else "DENY"
        print(f"#{r['id']} run={r['run_id']} {verdict} {r['action']:12} {r['detail'][:100]}")
    return 0



def cmd_runs(args) -> int:
    runs = db.list_runs(limit=args.limit)
    if args.status:
        runs = [r for r in runs if r.status == args.status]
    if args.grep:
        g = args.grep.lower()
        runs = [r for r in runs if g in r.goal.lower()]
    if not runs:
        print("no runs matched")
        return 0
    for r in runs:
        mode = "dry" if r.dry_run else "exec"
        started = r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "?"
        print(f"#{r.id:<4} {started} {r.status:<15} {mode:<4} {r.provider or '-':<9} "
              f"steps={r.steps_total or r.steps_done:<3} ${r.cost_usd:<7.4f} {r.goal[:60]}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vtai", description="VT-AIAgent CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Show provider, paths, and guardrail config").set_defaults(func=cmd_info)
    sub.add_parser("tools", help="List registered tools").set_defaults(func=cmd_tools)
    sub.add_parser("doctor", help="Preflight checks: is this machine set up?").set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="Create/validate config, writable roots, data dir")
    i.add_argument("--force", action="store_true", help="Overwrite existing config")
    i.set_defaults(func=cmd_init)

    r = sub.add_parser("run", help="Invoke a tool directly (JSON params)")
    r.add_argument("tool", help="Tool name, e.g. organize_files")
    r.add_argument("--params", default="{}", help='JSON args, e.g. \'{"directory":"~/Downloads"}\'')
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("goal", help="Give the agent a natural-language goal")
    g.add_argument("goal", help='e.g. "Organize ~/Downloads and report disk usage"')
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                   help="Preview actions only (overrides config default)")
    g.add_argument("--execute", dest="dry_run", action="store_false",
                   help="Allow real changes (overrides config default)")
    g.set_defaults(func=cmd_goal)

    r2 = sub.add_parser("runs", help="List recent runs")
    r2.add_argument("--limit", type=int, default=20)
    r2.add_argument("--status", help="ok | failed | aborted | budget_exceeded | running")
    r2.add_argument("--grep", help="Case-insensitive substring on goal")
    r2.set_defaults(func=cmd_runs)

    ap = sub.add_parser("approve",
                        help="Resolve pending delegate_coding approvals")
    ap.add_argument("approval_id", nargs="?", type=int, default=None,
                    help="Pending approval id (see --list)")
    ap.add_argument("--rollback", action="store_true",
                    help="Roll the repo back instead of keeping changes")
    ap.add_argument("--list", action="store_true",
                    help="List pending approvals")
    ap.set_defaults(func=cmd_approve)

    a = sub.add_parser("audit", help="List recent audit-log entries")
    a.add_argument("--limit", type=int, default=50)
    a.add_argument("--action", help="Filter by action (shell, write, delegate, ...)")
    a.add_argument("--run-id", type=int, default=None)
    a.add_argument("--allowed", dest="allowed", action="store_true", default=None,
                   help="Only show allowed actions")
    a.add_argument("--denied", dest="allowed", action="store_false",
                   help="Only show denials")
    a.add_argument("--grep", help="Case-insensitive substring on detail")
    a.set_defaults(func=cmd_audit)

    sv = sub.add_parser("serve", help="Run the MCP server (stdio or HTTP)")
    sv.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--token", default=None,
                    help="Bearer token (or VT_HTTP_TOKEN). Required for non-localhost binding.")
    sv.set_defaults(func=cmd_serve)

    vs = sub.add_parser("voice-sync", help="Install/update agent-listen & agent-speak from scripts/")
    vs.add_argument("--dest", default="~/.local/bin",
                    help="Install directory (must be on PATH)")
    vs.add_argument("--check", action="store_true",
                    help="Report drift without copying (exit 1 when out of sync)")
    vs.set_defaults(func=cmd_voice_sync)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command in ("goal", "info"):
        try:
            swept = db.sweep_orphaned_runs()
            if swept:
                print(f"[sweep] marked {swept} orphaned run(s) failed")
        except Exception as e:
            print(f"[sweep] skipped: {e}", file=sys.stderr)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
