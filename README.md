<!-- mcp-name: io.github.velsrocky/vtai-agent -->
# VT-AIAgent

[![CI](https://github.com/velsrocky/vtai-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/velsrocky/vtai-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

System-automation agent for **your** machine, exposed as an MCP server for
opencode / Claude Code / pi — and drivable directly via the `vtai` CLI.

## Tools

| Tool | What it does |
|---|---|
| `system_info` | Read-only machine snapshot (CPU, RAM, disks, load, GPU) plus the config and guardrails in effect |
| `run_shell` | Allowlisted shell commands, sandboxed (see below) |
| `organize_files` | Content-aware directory cleanup inside writable roots |
| `backup_sync` | rsync incremental backup with verification; `--delete` is opt-in |
| `transcode_media` | VAAPI-accelerated batch video transcode; never touches originals |
| `disk_audit` | Read-only scan for reclaimable disk space |
| `delegate_coding` | Hand tasks to opencode/claude/pi; git snapshot + verify + rollback; require human approval by default |
| `auto_mode` | Autonomous loop: hand a goal to a coding CLI, review the reply, iterate until it matches |
| `inspect_application` | REA-powered static analysis of an app under guardrails: deny-glob check → quarantine copy → minimal-env run → evidence digest in the audit trail → optional judge pass (off until enabled in config) |

## Safety model

- **Dry-run by default** (`guardrails.dry_run_default`) — previews never touch disk.
- **Path containment**: writes only inside `paths.writable_roots`, resolved
  paths defeat `..`/symlink escapes; `guardrails.deny_globs` protect secrets.
- **Fail-closed shell sandbox**: the binary must be a bare allowlisted name
  with an explicit read/write policy, resolve inside `trusted_bin_dirs`, carry
  no exec-enabling flags, and every path argument passes the same checks.
- **Kill switch**: drop a `KILL` file in `data_dir` to abort executing tools.
- **Echo-loop breaker**: N identical denials in a window escalate the refusal
  into a `CIRCUIT BREAKER` instruction the looping model can't miss.
- **Budget cap**: `budget_usd_per_run` aborts an orchestrated run when accrued
  token cost exceeds it; spend is recorded per run.
- **Audit**: every allow and deny is logged to SQLite in `data_dir`, each
  invocation (orchestrated, MCP, or CLI) attributed to a run row.

## Quick start

### Prerequisites

| | Requirement | Notes |
|---|---|---|
| **OS** | Linux, macOS, or Windows | all three run in CI (blocking since 0.6.0) |
| **Python** | 3.14+ | pinned by `.python-version`; `uv` fetches it for you |
| **uv** | >= 0.13.1 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Planner LLM** | one of: Ollama, any OpenAI-compatible endpoint, or a cloud key | see [Choosing a model](#choosing-a-model) |
| *rsync* | optional | required by `backup_sync`; without it the tool fails closed |
| *ffmpeg* | optional | required by `transcode_media` and the voice helpers |
| *Lemonade Server* | optional | speech in/out for `agent-listen` / `agent-speak` |

### 1. Install

```bash
git clone https://github.com/velsrocky/vtai-agent.git
cd vtai-agent
uv sync --dev
```

`uv sync` needs uv >= 0.13.1 (the lockfile is `revision = 5`; the floor is
declared as `[tool.uv] required-version`, so an older uv says so plainly
instead of failing on a lockfile parse).

### 2. Configure

The agent reads one TOML file. **Which** one is resolved in this order:

1. `$VT_CONFIG` — explicit override
2. `config/settings.toml` next to the package (i.e. the clone you just made)
3. `~/.vtaiagent/settings.toml` — the per-user file

A fresh clone contains the **maintainer's** config (their provider, their
writable roots), so point the agent at your own file instead of editing the
tracked one:

```bash
export VT_CONFIG="$HOME/.vtaiagent/settings.toml"
uv run vtai init          # writes a starter config there + creates the dirs
```

Put that `export` in your shell profile so every later command (and the MCP
server) picks it up.

`vtai init` creates `~/.vtaiagent` (audit DB + data dir), `~/Downloads`, and
`~/Documents/vt-data`, and writes a starter config with safe defaults:
`dry_run_default = true`, `deny_globs` for `~/.ssh`, `~/.gnupg`, `~/.aws` and
`**/.env`, and writable roots limited to `~/Downloads`, `~/Documents/vt-data`
and `~/.vtaiagent/tmp`. It is safe to re-run.

Check which config you ended up with — `vtai info` names the file and its
origin:

```bash
uv run vtai info
#   config file     : /Users/you/.vtaiagent/settings.toml (user-level)
```

<details>
<summary>Alternative: keep the checkout's config and override with env vars</summary>

A `.env` file (gitignored; copy `.env.example`) in the working directory
overrides any setting, using the `VT_` prefix with `__` for nesting:

```bash
VT_LOCAL__BASE_URL=http://localhost:13305/api/v1
VT_LOCAL__MODEL=<a pulled chat model>
VT_ACTIVE_PROVIDER=local
```

This is how a machine with no Ollama points the agent at a Lemonade server
instead. The `.env` is read from the **current working directory**, so run
the CLI from the checkout, or export the variables in your shell.
</details>

### Choosing a model

The planner/judge model comes from `active_provider`:

- `local` — any OpenAI-compatible endpoint: Ollama (`http://localhost:11434/v1`),
  llama.cpp, vLLM, LM Studio, Lemonade. No API key.
- `anthropic` / `openai` — cloud models; put `ANTHROPIC_API_KEY` or
  `OPENAI_API_KEY` in `.env`.

Set `cost_per_1k_input` / `cost_per_1k_output` in the provider block to make
`budget_usd_per_run` meaningful; without them the agent falls back to the
model's native price table.

### 3. Verify

Start with the preflight — it checks uv, your config, the writable roots,
what is protected, the optional tools, the model endpoint, the MCP
registration and any delegate waiting for review:

```bash
uv run vtai doctor
```

```
VT-AIAgent doctor — v0.6.2
  ✓  agent version      0.6.2
  ✓  uv                 0.13.1
  ✓  config file        /Users/you/.vtaiagent/settings.toml (user-level)
  ✓  config valid       no issues
  ✓  writable roots     3 present
  ✓  protected paths    4 deny globs
  ✓  shell allowlist    22 binaries
  ✓  model endpoint     http://localhost:11434/v1
  !  optional tools     not installed: ffmpeg (for transcode_media, voice helpers)
  ✓  mcp registration   opencode (global)
  ✓  pending approvals  none
```

It exits non-zero if something needs fixing. Then, to try the pieces:

```bash
uv run vtai info                         # config, provider, guardrails
uv run vtai tools                        # the registered tools
uv run vtai run system_info              # a read-only call, no LLM involved
uv run vtai goal --dry-run "list my Downloads"   # full loop with the model
```

### 4. Use it from your agent CLI

```bash
scripts/register-mcp.sh                  # opencode (global), claude (user), pi (CLI)
```

opencode's registration lands in `~/.config/opencode/opencode.jsonc` and
merges with your existing providers. The script registers an **absolute**
`uv` path on purpose: a bare `uv` fails with
`NotFound: ChildProcess.spawn (uv ...)` when the client spawns the server
from a daemon with a minimal PATH.

### Use

```bash
uv sync                                  # install
uv run vtai init                         # create/validate config, dirs, writable roots
uv run vtai info                         # show config
uv run vtai tools                        # list tools
uv run vtai run system_info              # invoke a tool
uv run vtai run run_shell --params '{"command":"ls ~/Downloads","dry_run":true}'
scripts/register-mcp.sh                  # register with opencode / claude
```

To get the `vtai` CLI outside the checkout, install it as an editable tool so
it tracks your working copy:

```bash
uv tool install --editable /path/to/vtai-agent   # -> ~/.local/bin/vtai
```

Install into another project as a dependency with
`uv add git+https://github.com/velsrocky/vtai-agent@v0.6.1`, or pull the
image with `docker build -t vtai-agent .`.

## Registry & distribution

PyPI: `uv tool install vtai-agent` (or `pipx install vtai-agent`) — publishes
on `v*` tags via Trusted Publishing with build attestations (see
`.github/workflows/publish.yml`). The `<!-- mcp-name: ... -->` comment above
is the PyPI-package proof the official MCP registry expects; `server.json`
carries the registry manifest (`io.github.velsrocky/vtai-agent`, stdio)
alongside a metadata-consistency gate in the publish workflow. Only install
from these sources; a similar repository name does not establish provenance.

Register with the official MCP registry after a PyPI release:
```bash
mcp-publisher auth --oauth2-github
mcp-publisher publish server.json
```

## Certification (vtai-cert)

Safety claims are probes, not prose: `vtai certify` runs the executable
guardrail-compliance suite (deny-glob containment, dotdot/symlink escapes,
fail-closed shell, injected-argv resistance, dry-run-first, kill switch,
audit exactness, echo-loop breaker) and emits a digest-stamped certificate;
exit 0 gates the release in CI. See [docs/certification.md](docs/certification.md).

## HTTP transport

`vtai serve --transport http --port 8765 --token $VT_HTTP_TOKEN` exposes the
same guarded tools over MCP StreamableHTTP at `/mcp`. The bearer token is
required when binding anything other than localhost; the server refuses to
bind externally without one.

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
uv run ruff check src/ tests/
uv run python scripts/generate_tool_catalog.py --check   # tool-catalog doc drift
```

The config file is resolved as described in [Configure](#2-configure): `$VT_CONFIG`
→ the checkout's `config/settings.toml` → `~/.vtaiagent/settings.toml`. Env
overrides use the `VT_` prefix with `__` nesting (e.g.
`VT_GUARDRAILS__DRY_RUN_DEFAULT=false`). Config is validated at startup
(`validate_settings`) — a bad `deny_globs` pattern or a missing writable root
fails fast, not mid-run.

**Windows:** the shell sandbox needs the allowlisted binaries to resolve, and a
fresh Windows runner does not put Git for Windows' coreutils on PATH. Install
Git for Windows and prepend its `usr\bin` (this is what CI does):

```powershell
Add-Content $env:GITHUB_PATH "C:\Program Files\Git\usr\bin"   # CI
# locally: add "C:\Program Files\Git\usr\bin" to your PATH
```

Everything else (install, config, MCP registration) is the same on all three
platforms.

## Orchestrator

`vtai goal` runs a bounded ReAct-style agent loop. Each step the model
either emits one fenced-JSON tool call (parsed, guarded, executed, result fed
back) or a final natural-language answer. Bounds enforced every step:
`max_steps`, kill switch, and `budget_usd_per_run`. A lonely `dry_run: true`
text blob that fails to parse or misses its `tool` key is retried with a
corrective note instead of ending the run as "success"; the run mode is
authoritative — the model cannot override it — and every tool call is
persisted as a `Step` row and audit entry. If a paid provider's spend can't
be determined (no `cost_per_1k_*` config and no native cost), the run fails
closed rather than reporting free. The system prompt describes the machine's
actual OS.

We use a text protocol rather than native function-calling on purpose: the
local Ollama models on this box emit tool calls as raw JSON text even when
native tool schemas are provided (verified 2026-10-03 with qwen2.5-coder:7b),
so the text protocol behaves identically across local and cloud providers.

## Auto mode

`auto_mode` runs the loop you describe: it dispatches the goal to a coding
CLI (opencode by default), reviews the reply against the goal with the
configured model, and if the reply is insufficient, sends a follow-up prompt
quoting the critique — up to `max_iterations`. The judge only accepts a bare
`OK` as satisfied (hedged replies fail). Spend is metered against
`guardrails.budget_usd_per_run` and attributed in the run's `spend` detail:
the judge's own LLM calls always; the delegate CLI when it can report it —
`claude -p --output-format json` exposes `total_cost_usd`, and an unreadable
report from a cost-capable CLI fails closed. `opencode`/`pi` report no
headless spend (documented vendor behavior, not guess-parsed event streams),
so their runs carry `"cli_usd": "unmeasured"` and stay bounded only by
`max_iterations`/`timeout`. From inside opencode or
Claude Code, start the prompt with `@vt-ai_automode_on`: the MCP server's
instructions route that to the tool.

```bash
uv run vtai run auto_mode --params '{"goal":"add a unit test for parser.py","working_dir":".","max_iterations":3}'
```

## Delegate coding

`delegate_coding` snapshots the target repo, runs the chosen CLI headlessly,
captures the diff, and on `verify_command` failure (or timeout / non-zero
exit) rolls back. Three guards worth knowing:

- **Human approval gate**: `require_approval` defaults to `true`. Interactively
  it shows `git diff --stat` and asks before keeping changes. Non-interactive
  runs (MCP, scripted, orchestrated) keep the changes in the worktree as a
  **pending approval** — the run reports failure so automated loops stop — and
  `vtai approve <id>` (keep) or `vtai approve <id> --rollback` resolves it.
  While a pending approval exists for a repo, delegate_coding refuses new
  work there, even with `require_approval: false`. Pass `require_approval:
  false` only for tasks a human has already reviewed.
- **`pi` needs credentials**: with no authenticated provider, `pi -p` exits 0
  having emitted a tool-call JSON block and applied nothing. The tool now
  fails closed with a clear message. Run `pi auth check` — or prefer
  `opencode` / `claude` — before delegating to pi.

Claude delegations run with `--output-format json`, so the reply text comes
from `result` and the invocation's `total_cost_usd` is captured into the
tool detail as `cli_cost_usd` (and audited). Delegations to opencode/pi
report `"cli_cost_usd": "unmeasured"`.

```bash
uv run vtai approve          # list pending delegate approvals
uv run vtai approve 7        # keep the pending diff
uv run vtai approve 7 --rollback
```

`vtai info` shows how many approvals are waiting, so a delegate that was kept
for review does not get lost.

## Inspect applications (REA capability)

`inspect_application` integrates [REA](https://github.com/morluto/rea) so the
agent can statically explain how a local app works without source code.
Because REA's inspection is powerful, it runs entirely inside vtai-agent's
guards and is **off by default**:

```toml
[capabilities.rea]
enabled        = true             # enable only after install vetting
binary         = "rea"
command_prefix = ["analyze-javascript-application"]
timeout        = 1800.0
keep_copy      = false
```

Safety contract, in order: the target must pass `deny_globs`; it is then
copied into a quarantine dir under `data_dir` (denied entries and
out-of-policy symlinks are skipped on the way in, so REA never reads a path
you couldn't); REA runs on the copy with a minimal child environment (no
inherited API keys) and is killed after `timeout`; output JSON is parsed and
a sha256 evidence digest plus REA's evidence id are written to the audit
trail; the optional judge pass (same model and fail-closed policy as
auto_mode) verifies the evidence answers the goal before the result reports
success. The original target is never touched, and the quarantine copy is
deleted unless `keep_copy` is true.

```bash
uv run vtai run inspect_application \
  --params '{"goal":"explain how Notes builds its CSV export header",
             "target":"~/quarantine/Notes-app","dry_run":true}'
```

Install REA only from official sources (github.com/morluto/rea or the
`rea-agents` package on npm — verify provenance), not mirror sites.

## Audit log

Every allow/deny is stored in `~/.vtaiagent/vtai.db`. Query it without a SQL
client:

```bash
uv run vtai audit --limit 20
uv run vtai audit --denied --grep ssh
uv run vtai audit --action delegate --run-id 193
```

To see one run end to end — the goal, every tool call, what was allowed or
refused, and the spend:

```bash
uv run vtai runs            # list runs
uv run vtai show 193        # one run in full
```

## Voice control (`agent-listen` / `agent-speak`)

Both helpers talk to a local Lemonade Server (TTS `kokoro-v1`, STT
`Whisper-Tiny` on Linux/macOS — Windows defaults to `Moonshine-Tiny-Streaming`
because the whisper.cpp recipe misbehaves on some AMD GPUs) at
`http://localhost:13305`. `LEMONADE_STT_MODEL` overrides the STT model on any
platform. Use `--seconds N` to change the
record window, `--vad` for silence-based early stop, `LEMONADE_BASE_URL` to
point elsewhere.

The scripts are versioned in `scripts/`; install them with:

```bash
uv run vtai voice-sync
```

`voice-sync` copies both helpers to `~/.local/bin` (override with `--dest`),
marks them executable, and reports `installed` / `updated` / `unchanged` per
file. `vtai voice-sync --check` only reports drift (exit 1 when out of sync)
— re-run without `--check` after pulling script updates. Set
`VTAI_SCRIPTS_DIR` to sync from a different checkout.

Cross-platform notes (verified on macOS 27/ARM, 2026-10-12):

> **Supported: Linux + macOS + Windows** — all three run in CI (Linux and
> macOS push-blocked; Windows reinstated in 0.6.0 after the suite became
> portable, with Git Bash coreutils prepended to PATH so the allowlisted
> sandbox resolves real binaries). rsync-dependent tests skip on runners
> without rsync and are complemented by a fail-closed counter-test.

- `arecord` is used for recording on Linux; elsewhere the helpers fall back
  to `ffmpeg` (`-f pulse` / `avfoundation`), so macOS and Windows work too.
- `--vad` currently requires Linux `arecord`; on other platforms use
  `--seconds N`.
- macOS mic selection: avfoundation device indexes vary per machine — list
  them with `ffmpeg -f avfoundation -list_devices true -i ""` and override
  via `AGENT_LISTEN_FF_INPUT` (default audio-only `-f avfoundation -i ":0"`).
  Machines with no built-in microphone (many mini PCs) need an external/USB
  device attached, otherwise recording yields silence (exit 6, no speech).
- Playback tries `ffplay` → `afplay` (macOS) → `mpg123` → `aplay` in order.
- The ALSA mic-level fix and `vtai-audio-fix` systemd unit below are
  Linux-specific; Windows and macOS handle this in System Settings.
- `guardrails.trusted_bin_dirs` defaults per OS (`C:\Windows\System32`,
  `/usr/bin, /bin, /usr/local/bin, /opt/homebrew/bin`, and the Linux set);
  entries missing on the current machine are ignored (validation only fails
  when none exist), so one config can list both Linux and macOS paths.
- `system_info` is portable: memory via `sysctl`/`vm_stat` and disks via
  `/`, `$HOME`, `/Volumes/*` on macOS (Metal GPU note; no `rocm-smi` there).
- `transcode_media` defaults to `libx264` off Linux (VAAPI is Linux-only);
  an explicit `"codec"` still overrides.
- `disk_audit` also measures `~/Library/Caches`, `~/Library/Logs`,
  `/Library/Caches`, and `~/.Trash` on macOS; fresh mac installs get
  `vm_stat` (instead of Linux-only `free`/`nproc`) in the starter allowlist.

### Troubleshooting

**Setup and MCP**

- **`opencode mcp list` shows `✗ vtai-agent failed: NotFound: ChildProcess.spawn (uv ...)`**:
  the client spawned the server with a minimal PATH (daemons give
  `PATH=/usr/bin:/bin:...`), so a bare `uv` was unresolvable. Re-run
  `scripts/register-mcp.sh` — it registers the absolute `uv` path — or edit
  the `mcp.servers.vtai-agent.command` array in your opencode config to the
  full path of `uv` (`command -v uv`).
- **`opencode mcp list` says `No MCP servers configured`**: the registration
  is per-project. Run it from the checkout, or use the global registration
  `scripts/register-mcp.sh` writes to `~/.config/opencode/opencode.jsonc`.
- **"config exists: …/config/settings.toml" and your settings are ignored**:
  the checkout's config wins over `~/.vtaiagent/settings.toml`. Set
  `VT_CONFIG="$HOME/.vtaiagent/settings.toml"` and re-run `vtai init`;
  `vtai info` prints the file in use and where it came from.
- **`uv sync` fails with `Required uv version ... does not match`**: install
  uv >= 0.13.1 (the lockfile is revision 5).

**Voice (Linux / PipeWire / ALSA)**

- **Mic produces silence or clipped garbage transcripts**: the internal mic
  boost is pinned too high and clips the input. Fix (also applied at
  every login via `~/.config/systemd/user/vtai-audio-fix.service`):

  ```bash
  amixer -c 2 sset 'Internal Mic Boost' 0
  amixer -c 2 sset 'Capture' 75%
  ```

- **Headphone jack not detected**: the kernel reports no jack on the combo
  port, so the headphone amp stays off. Check `pw-cli enum-params 53
  EnumRoute` — the `analog-output-headphones` route shows `available: no`
  until a headset is firmly plugged in. Route manually with
  `wpctl set-route 38 1` if detection is flaky.

- **Default source is the wrong mic**: list routes with
  `pw-cli enum-params 53 Route` and select
  with `wpctl set-route 47 <index>` (`0` = internal mic, `1` = headset mic).

- **STT returns `[BLANK_AUDIO]` or noise transcripts**: verify levels with
  `arecord -d 3 -f cd /tmp/t.wav` and inspect the peak; near 0 means the
  selected route carries no signal, full-scale means clipping (lower the
  boost).

**Delegate coding**

- **Files changed via `delegate_coding` are still sitting in the repo**: that
  is a pending approval — the changes are kept for your review until you run
  `vtai approve <id>` to keep them or `--rollback` to discard them. Pass
  `require_approval: false` only for tasks a human has already reviewed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — setup, the guardrail rules any
change has to respect, and the test/lint commands.

## Security

Guardrails, what counts as a vulnerability, and how to report one privately:
[SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, run it on your own machine.
