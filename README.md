# VT-AIAgent

System-automation agent for this machine, exposed as an MCP server for
opencode / Claude Code / pi — and drivable directly via the `vtai` CLI.

## Tools

| Tool | What it does |
|---|---|
| `system_info` | Read-only machine snapshot (CPU, RAM, disks, load, AMD GPU) |
| `run_shell` | Allowlisted shell commands, sandboxed (see below) |
| `organize_files` | Content-aware directory cleanup inside writable roots |
| `backup_sync` | rsync incremental backup with verification; `--delete` is opt-in |
| `transcode_media` | VAAPI-accelerated batch video transcode; never touches originals |
| `disk_audit` | Read-only scan for reclaimable disk space |
| `delegate_coding` | Hand tasks to opencode/claude/pi; git snapshot + verify + rollback |

## Safety model

- **Dry-run by default** (`guardrails.dry_run_default`) — previews never touch disk.
- **Path containment**: writes only inside `paths.writable_roots`, resolved
  paths defeat `..`/symlink escapes; `guardrails.deny_globs` protect secrets.
- **Fail-closed shell sandbox**: the binary must be a bare allowlisted name
  with an explicit read/write policy, resolve inside `trusted_bin_dirs`, carry
  no exec-enabling flags, and every path argument passes the same checks.
- **Kill switch**: drop a `KILL` file in `data_dir` to abort executing tools.
- **Audit**: every allow and deny is logged to SQLite in `data_dir`.

## Use

```bash
uv sync                                  # install
uv run vtai info                         # show config
uv run vtai tools                        # list tools
uv run vtai run system_info              # invoke a tool
uv run vtai run run_shell --params '{"command":"ls ~/Downloads","dry_run":true}'
scripts/register-mcp.sh                  # register with opencode / claude
```

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
uv run ruff check src/ tests/
```

Config lives in `config/settings.toml`; env overrides use the `VT_` prefix
with `__` nesting (e.g. `VT_GUARDRAILS__DRY_RUN_DEFAULT=false`).
