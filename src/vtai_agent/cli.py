import argparse
import asyncio
import json
import sys

from . import __version__, db
from .config import get_settings
from .providers import describe_active
from .tools import registry
from .mcp_server import register_tools


STARTER_TOML = """\
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
  "ls", "cat", "head", "tail", "wc", "du", "df", "free", "grep",
  "file", "stat", "mkdir", "mv", "cp", "rsync", "tar", "zip", "unzip",
  "nproc", "uptime", "pytest",
]
deny_globs = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "**/.env"]

[guardrails.shell_extra_modes]
pytest = "write"

[paths]
writable_roots = ["~/Downloads", "~/Documents/vt-data", "~/.vtaiagent/tmp"]
data_dir = "~/.vtaiagent"
"""


def cmd_init(args) -> int:
    """Bootstrap: create config/settings.toml (if missing), writable roots,
    and the data dir, then validate. Safe to re-run."""
    from pathlib import Path

    from .config import CONFIG_TOML, Settings, validate_settings

    rc = 0
    if CONFIG_TOML.exists() and not args.force:
        print(f"config exists: {CONFIG_TOML} (use --force to regenerate)")
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
    s = get_settings()
    print(f"VT-AIAgent v{__version__}")
    print(f"  active provider : {describe_active(s)}")
    print(f"  data dir        : {s.paths.data_dir}")
    print(f"  writable roots  : {[str(p) for p in s.paths.writable_roots]}")
    print(f"  dry_run_default : {s.guardrails.dry_run_default}")
    print(f"  shell allowlist : {s.guardrails.shell_allowlist}")
    return 0


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

    app = build_server().streamable_http_app(host=args.host)

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
