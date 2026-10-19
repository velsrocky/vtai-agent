# Deployment Guide

VT-AIAgent is an MCP stdio server plus a CLI. There is no daemon, web
dashboard, or HTTP port — your agent CLI (opencode / Claude Code) spawns the
server on demand.

## Prerequisites

- **OS:** Linux, macOS, or Windows (all three are covered by CI; Windows
  needs Git for Windows' coreutils on PATH — see below)
- **Python:** 3.14+ (uv fetches it for you)
- **uv:** >= 0.13.1 — https://docs.astral.sh/uv/ —
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Planner LLM:** Ollama, any OpenAI-compatible endpoint (llama.cpp, vLLM,
  LM Studio, Lemonade), or a cloud provider key
- **Optional:** `rsync` (for `backup_sync`), `ffmpeg` (for `transcode_media`
  and the voice helpers), a Lemonade Server (for voice)

## Installation

```bash
git clone https://github.com/velsrocky/vtai-agent.git
cd vtai-agent
uv sync
```

The lockfile is `revision = 5`, which needs uv >= 0.13.1. The floor is
declared in `pyproject.toml` (`[tool.uv] required-version`), so an older uv
reports `Required uv version ... does not match` instead of failing to parse
the lockfile.

### Windows

The shell sandbox executes allowlisted binaries, and a stock Windows install
has `cat`/`cp`/`mkdir` only inside Git for Windows. Install Git for Windows
and put its `usr\bin` on PATH (this is what CI does):

```powershell
Add-Content $env:GITHUB_PATH "C:\Program Files\Git\usr\bin"
```

## Configuration

One TOML file drives everything. It is resolved in this order:

1. `$VT_CONFIG` — explicit override
2. `config/settings.toml` next to the package (the clone)
3. `~/.vtaiagent/settings.toml` — the per-user file

**A fresh clone ships the maintainer's config**, so point the agent at your
own file rather than editing the tracked one:

```bash
export VT_CONFIG="$HOME/.vtaiagent/settings.toml"
uv run vtai init          # writes a starter config there, creates the dirs
uv run vtai info          # prints the config file in use and its origin
```

`vtai init` is safe to re-run. It creates `~/.vtaiagent` (audit DB, data
dir), `~/Downloads`, `~/Documents/vt-data` and a starter config:

```toml
[guardrails]
dry_run_default    = true
max_steps          = 40
budget_usd_per_run = 1.0
shell_allowlist    = ["ls", "cat", "cp", ...]   # bare names with sandbox policy
deny_globs         = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "**/.env"]
trusted_bin_dirs   = ["/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]

[paths]
writable_roots = ["~/Downloads", "~/Documents/vt-data", "~/.vtaiagent/tmp"]
data_dir       = "~/.vtaiagent"   # sqlite audit db + KILL switch
```

Secrets live in `.env` (copy `.env.example`); env overrides use the `VT_`
prefix with `__` nesting, e.g. `VT_GUARDRAILS__DRY_RUN_DEFAULT=false`. The
`.env` is read from the current working directory.

## Running

### As an MCP server (recommended)

```bash
scripts/register-mcp.sh          # registers with opencode and Claude Code
```

opencode's registration goes to `~/.config/opencode/opencode.jsonc` and merges
with your existing providers; Claude Code's is user-scoped. The script
registers an **absolute** `uv` path on purpose — a bare `uv` fails with
`NotFound: ChildProcess.spawn (uv ...)` when the client spawns the server
from a daemon with a minimal PATH.

or manually in `opencode.json`:

```json
{
  "mcp": {
    "servers": {
      "vtai-agent": {
        "type": "local",
        "command": [
          "/opt/homebrew/bin/uv",
          "run",
          "--directory",
          "/path/to/vtai-agent",
          "vtai",
          "serve"
        ]
      }
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
uv run vtai approve            # pending delegate approvals
```

To use the CLI outside the checkout, install it as an editable tool so it
tracks your working copy:

```bash
uv tool install --editable /path/to/vtai-agent   # -> ~/.local/bin/vtai
```

### HTTP transport

```bash
vtai serve --transport http --port 8765 --token "$VT_HTTP_TOKEN"
```

Same guarded tools over MCP StreamableHTTP at `/mcp`. The bearer token is
required when binding anything other than localhost.

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
uv run pytest tests/ -v          # 109 tests incl. sandbox bypass attempts
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
- [ ] `VT_CONFIG` points at *your* config, not the checkout's
