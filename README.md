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

Requires **uv >= 0.13.1** (declared as `[tool.uv] required-version`; the
lockfile is revision 5).

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
with `__` nesting (e.g. `VT_GUARDRAILS__DRY_RUN_DEFAULT=false`). On a Mac
without Ollama, point the local provider at Lemonade instead via a
gitignored machine-local `.env`:
`VT_LOCAL__BASE_URL=http://localhost:13305/api/v1` plus
`VT_LOCAL__MODEL=<a pulled chat model>`. Config is
validated at startup (`validate_settings`) — a bad `deny_globs` pattern or a
missing writable root fails fast, not mid-run.

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

## Audit log

Every allow/deny is stored in `~/.vtaiagent/vtai.db`. Query it without a SQL
client:

```bash
uv run vtai audit --limit 20
uv run vtai audit --denied --grep ssh
uv run vtai audit --action delegate --run-id 193
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
  Note the Mac mini has no built-in microphone; an external/USB device must
  be present, otherwise recording yields silence (exit 6, no speech).
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

- **Files changed via `delegate_coding` are still sitting in the repo**: that
  is a pending approval — the changes are kept for your review until you run
  `vtai approve <id>` to keep them or `--rollback` to discard them. Pass
  `require_approval: false` only for tasks a human has already reviewed.
