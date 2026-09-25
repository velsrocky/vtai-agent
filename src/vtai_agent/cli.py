import argparse
import asyncio
import json
import sys

from . import __version__, db
from .config import get_settings
from .providers import describe_active
from .tools import registry
from .mcp_server import register_tools


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


def cmd_serve(_args) -> int:
    from .mcp_server import build_server
    build_server().run(transport="stdio")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vtai", description="VT-AIAgent CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Show provider, paths, and guardrail config").set_defaults(func=cmd_info)
    sub.add_parser("tools", help="List registered tools").set_defaults(func=cmd_tools)

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

    sub.add_parser("serve", help="Run the MCP server over stdio").set_defaults(func=cmd_serve)
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
