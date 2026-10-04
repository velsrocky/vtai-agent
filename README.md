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
| `delegate_coding` | Hand tasks to opencode/claude/pi; git snapshot + verify + rollback; require human approval by default |

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

## Use

```bash
uv sync                                  # install
uv run vtai init                         # create/validate config, dirs, writable roots
uv run vtai info                         # show config
uv run vtai tools                        # list tools
uv run vtai run system_info              # invoke a tool
uv run vtai run run_shell --params '{"command":"ls ~/Downloads","dry_run":true}'
scripts/register-mcp.sh                  # register with opencode / claude
```

Install into another project as a dependency with
`uv add git+https://github.com/velsrocky/vtai-agent@v0.5.0`, or pull the
image with `docker build -t vtai-agent .`.

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
```

Config lives in `config/settings.toml`; env overrides use the `VT_` prefix
with `__` nesting (e.g. `VT_GUARDRAILS__DRY_RUN_DEFAULT=false`). Config is
validated at startup (`validate_settings`) — a bad `deny_globs` pattern or a
missing writable root fails fast, not mid-run.

## Orchestrator

`vtai goal` runs a bounded ReAct-style agent loop. Each step the model
either emits one fenced-JSON tool call (parsed, guarded, executed, result fed
back) or a final natural-language answer. Bounds enforced every step:
`max_steps`, kill switch, and `budget_usd_per_run`. The run's dry-run mode is
authoritative — the model cannot override it — and every tool call is
persisted as a `Step` row and audit entry. If a paid provider's spend can't
be determined (no `cost_per_1k_*` config and no native cost), the run fails
closed rather than reporting free.

We use a text protocol rather than native function-calling on purpose: the
local Ollama models on this box emit tool calls as raw JSON text even when
native tool schemas are provided (verified 2026-10-03 with qwen2.5-coder:7b),
so the text protocol behaves identically across local and cloud providers.

## Delegate coding

`delegate_coding` snapshots the target repo, runs the chosen CLI headlessly,
captures the diff, and on `verify_command` failure (or timeout / non-zero
exit) rolls back. Two extra guards worth knowing:

- **Human approval gate**: `require_approval` defaults to `true`. Interactively
  it shows `git diff --stat` and asks before keeping changes; non-interactive
  runs (MCP, scripted) roll back instead of keeping edits. Pass
  `"require_approval": false` only when a human has already reviewed the plan.
- **`pi` needs credentials**: with no authenticated provider, `pi -p` exits 0
  having emitted a tool-call JSON block and applied nothing. The tool now
  fails closed with a clear message. Run `pi auth check` — or prefer
  `opencode` / `claude` — before delegating to pi.

## Audit log

Every allow/deny is stored in `~/.vtaiagent/vtai.db`. Query it without a SQL
client:

```bash
uv run vtai audit --limit 20
uv run vtai audit --denied --grep ssh
uv run vtai audit --action delegate --run-id 193
```

## Voice control (`agent-listen` / `agent-speak`)

Both helpers talk to a local Lemonade Server (STT `Whisper-Tiny`, TTS
`kokoro-v1`) at `http://localhost:13305`. Use `--seconds N` to change the
record window, `--vad` for silence-based early stop, `LEMONADE_BASE_URL` to
point elsewhere.

The scripts are versioned in `scripts/`; install them with:

```bash
cp scripts/agent-listen scripts/agent-speak ~/.local/bin/
chmod +x ~/.local/bin/agent-listen ~/.local/bin/agent-speak
```

Cross-platform notes:

- `arecord` is used for recording on Linux; elsewhere the helpers fall back
  to `ffmpeg` (`-f pulse` / `avfoundation`), so macOS and Windows work too.
- `--vad` currently requires Linux `arecord`; on other platforms use
  `--seconds N`.
- Playback tries `ffplay` → `afplay` (macOS) → `mpg123` → `aplay` in order.
- The ALSA mic-level fix and `vtai-audio-fix` systemd unit below are
  Linux-specific; Windows and macOS handle this in System Settings.
- `guardrails.trusted_bin_dirs` defaults per OS (`C:\Windows\System32`,
  `/usr/bin, /bin, /usr/local/bin, /opt/homebrew/bin`, and the Linux set);
  override in `settings.toml` for custom installs.
- `transcode_media`'s VAAPI path is Linux-only; on macOS/Windows pass
  `"codec": "libx264"` and omit the render-node device.

### Troubleshooting

- **Mic produces silence or clipped garbage transcripts**: the internal mic
  boost is pinned too high and clips the ALC294 input. Fix (also applied at
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

- **Files changed via `delegate_coding` are rolled back**: that's the
  fail-safe default — pass `require_approval: false` after you've reviewed
  the diff, or `verify_command` to gate on tests instead.
