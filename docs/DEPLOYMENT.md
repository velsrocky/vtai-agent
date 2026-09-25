# Deployment Guide

VT-AIAgent is an MCP stdio server plus a CLI. There is no daemon, web
dashboard, or HTTP port — your agent CLI (opencode / Claude Code) spawns the
server on demand.

## Prerequisites

- **OS:** Linux (Ubuntu 22.04+)
- **Python:** 3.14+
- **uv:** https://docs.astral.sh/uv/ — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Optional:** Ollama or any OpenAI-compatible endpoint for the local planner

## Installation

```bash
git clone https://github.com/your-org/vtai-agent.git
cd vtai-agent
uv sync
```

## Configuration

Edit `config/settings.toml` (checked into the repo — there is no `.example`):

```toml
[guardrails]
dry_run_default    = true
max_steps          = 40
budget_usd_per_run = 1.0
shell_allowlist    = ["ls", "cat", "cp", ...]   # bare names with sandbox policy
deny_globs         = ["~/.ssh/**", "**/.env", ...]
trusted_bin_dirs   = ["/usr/bin", "/bin", "/usr/local/bin"]

[paths]
writable_roots = ["~/Downloads", "~/Documents/vt-data", "/tmp/vtaiagent"]
data_dir       = "~/.vtaiagent"   # sqlite audit db + KILL switch
```

Secrets live in `.env` (copy `.env.example`); env overrides use the `VT_`
prefix with `__` nesting, e.g. `VT_GUARDRAILS__DRY_RUN_DEFAULT=false`.

## Running

### As an MCP server (recommended)

```bash
scripts/register-mcp.sh          # registers with opencode and Claude Code
```

or manually in `opencode.json`:

```json
{
  "mcpServers": {
    "vtai-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/vtai-agent", "vtai", "serve"]
    }
  }
}
```

The server speaks stdio; it must run on the machine it administers.

### From the CLI

```bash
uv run vtai info
uv run vtai tools
uv run vtai run disk_audit --params '{"path":"~/Downloads","min_size_mb":200}'
uv run vtai goal "Organize ~/Downloads" --dry-run
uv run vtai goal "Organize ~/Downloads" --execute
```

### Docker (stdio MCP)

```bash
docker build -t vtai-agent .
docker run -i --rm \
  -v ~/.vtaiagent:/root/.vtaiagent \
  -v ~/Downloads:/root/Downloads \
  vtai-agent
```

Only makes sense if you genuinely want the container to be the sandbox —
mount exactly the roots you allow.

## Verify

```bash
uv run pytest tests/ -v          # 30 tests incl. sandbox bypass attempts
uv run ruff check src/ tests/
uv run vtai info
uv run vtai run run_shell --params '{"command":"cat ~/.ssh/id_rsa"}'
# expected: [ERROR] GuardrailViolation: ... Protected path ...
```

## Operations checklist

- [ ] `writable_roots` narrowed to what the agent actually needs
- [ ] `deny_globs` cover every secrets directory on the box
- [ ] `trusted_bin_dirs` matches how coreutils are installed (check `which cat`)
- [ ] `dry_run_default = true` unless you have a reason not to
- [ ] `~/.vtaiagent/` is backed up (audit history lives there)
- [ ] You know the kill switch: `touch ~/.vtaiagent/KILL` aborts any executing
      tool immediately; remove the file to resume
- [ ] Audit review: `sqlite3 ~/.vtaiagent/vtai.db 'select * from auditlog order by id desc limit 20'`
