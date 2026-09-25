# VT-AIAgent — Build Log

Everything built and verified, phase by phase. Each entry records what was
delivered, the decisions behind it, and the bugs found during testing.

> **Status note (0.3.1):** Phases 6–8 (perception, absence mode, FastAPI web
> UI/tray) were **deleted** in the 0.3.1 security review — they were never
> registered with the MCP surface and the API/tray code was broken at import.
> History is kept below for the design lessons. On any machine that ran them:
> `systemctl --user disable --now vtai-agent vtai-agent-api`.

---

## Phase 0–1 — Foundation, guardrails, MCP

**Delivered:** project scaffolding, layered config, SQLite persistence,
guardrail core, tool registry, and the MCP server surface.

- **uv** project with `pydantic-ai`, `mcp`, `sqlmodel`, `pydantic-settings`.
- **Config layer** reads `config/settings.toml` with env overrides
  (`VT_` prefix, `__` nesting). API keys come from standard env vars.
- **Guardrails**: path allowlist (`writable_roots`), deny-globs for secrets,
  shell allowlist matched on `argv[0]`, dry-run-by-default, kill switch.
- **SQLite models**: `Run`, `Step`, `AuditLog` — every action is replayable.
- **MCP server** over stdio; registered with opencode and Claude Code.

**Key decision — MCP hub, not a standalone app.** Since the agent is driven
from opencode/Claude Code/pi, it exposes its tools as an MCP server. One
registration line and every tool becomes native to those CLIs.

**Bugs found and fixed:**
- Path validator choked on `list` inputs (`writable_roots` received a list but
  tried to wrap it in `Path()`). Also defaults weren't expanded, so `~` would
  create a literal `~` directory. Fixed with `validate_default=True` and a
  validator that handles both list and scalar forms.
- TOML sections nested under `[provider]` were silently ignored —
  pydantic-settings maps top-level tables to field names, so `[paths]` loaded
  while `[provider]` did not. Restructured to top-level sections
  (`[anthropic]`, `[openai]`, `[local]`).

---

## Phase 2 — The agent loop

**Delivered:** `orchestrator.py` — a bounded ReAct loop, plus the `vtai goal`
command.

- Model-agnostic **text-based ReAct protocol**: the model emits tool calls as
  fenced JSON; the loop parses, guards, executes, and feeds results back.
- Bounds enforced each step: `max_steps`, `max_retries`, kill switch.
- Every step logged to SQLite with its input, output, and status.
- `vtai goal "..."` with `--dry-run` / `--execute` flags.

**Key decision — text protocol over native function-calling.** The local
model silently ignores function-call schemas and emits tool calls as text.
A text protocol behaves identically on every provider.

**Key decision — run mode is authoritative.** The model, told to execute,
still passed no `dry_run` arg — defaulting to preview — then *hallucinated*
that files had moved. The orchestrator now clamps `dry_run` based on run mode
at the tool layer: an execute decision by the operator can never be
downgraded by the model.

**Bugs found and fixed:**
- `DetachedInstanceError` — reading `run.id` after the session closed.
  Fixed by returning the id from inside `session_scope`.

---

## Phase 3 — Backup, media, disk workflows

**Delivered:** `backup_sync`, `transcode_media`, `disk_audit`.

- **`backup_sync`** — rsync incremental with checksum verification.
  `--delete` is opt-in *and* mode-gated: requested in a dry run, it's
  suppressed with a note rather than honored.
- **`transcode_media`** — batch transcoding on the RX 6700 XT via VAAPI
  (h264/hevc/av1/vp9), with software fallback.
- **`disk_audit`** — read-only finder of large/old files and cache sizes.

**Key decision — output never lands beside source.** Transcoded files go to a
`transcoded/` subfolder that is itself skipped as a scan source, so the agent
cannot re-encode its own output or clobber originals. Verified: re-running on
the same directory correctly finds 0 jobs.

**Bugs found and fixed:**
- `rsync --itemize` doesn't exist in 3.4.1 — it's `--itemize-changes`.
- Stats regex didn't handle comma-formatted sizes (`22,288` bytes).
- `os.DirEntry.path` returns `str`, breaking the disk walk — wrapped in `Path()`.

---

## Phase 5 — CLI delegation

**Delivered:** `delegate_coding` — hand coding work to opencode/claude/pi
headlessly, with verification and rollback.

- git snapshot before the delegate runs; diff captured for the audit log.
- Optional `verify_command` must pass, otherwise **auto-rollback**.
- Timeout path kills the delegate and restores the snapshot.

**CLI headless status (diagnosed by testing):**

| CLI | Headless | Notes |
|---|---|---|
| **opencode** | works | `opencode run --dir` — executes edits, clean diffs |
| **pi** | partial | works once configured, but the local model emits tool calls as text |
| **claude** | needs key | `claude -p` confirmed; requires `ANTHROPIC_API_KEY` |

**System changes made (reversible):**
- pi's `local-llama` provider pointed at a llama.cpp server on `:8080` that
  wasn't running — every invocation failed with "Connection error". Repointed
  to the running Ollama. Backup at `~/.pi/agent/models.json.bak`.
- Added test runners/build tools to the shell allowlist (`python3`, `pytest`,
  `cargo`, …). Without these, delegated fixes were rolled back because the
  *verification* itself was blocked.

**Bugs found and fixed:**
- Verification failed on a `verify.py` that imported from stdlib `math`
  instead of the local `math.py` — a name collision in the test sandbox,
  not an agent bug.

**Operational lesson:** a cleanup command deleted a directory another command
was still using, causing a confusing run of `FileSystem.access` errors. Worth
remembering for the scheduler: cleanup jobs must never run against
directories with in-flight work.

---

## Phase 6 — Perception *(deleted 0.3.1)*

**Delivered:** `screen_capture`, `analyze_screen`, `desktop_inspect`.

- **Wayland-safe screenshots** via `xdg-desktop-portal`, which requires
  listening for an async D-Bus `Response` signal rather than a blocking call.
- **Vision analysis** through the local `minicpm-v` on Ollama — zero cost per
  call, which makes continuous screen-watching affordable for absence mode.
- **AT-SPI2** accessibility tree: apps, UI trees, actionable elements —
  element names and roles instead of pixel-guessing.

**Key decision — sanctioned Wayland paths.** X11 tools (xdotool, scrot) don't
work under Wayland's security model. The portal and AT-SPI are the supported
alternatives, and AT-SPI is *better* than pixel-guessing anyway.

**Key decision — system-Python bridges.** `pygobject` can't build in the
project venv (needs dev headers), so AT-SPI and the portal signal loop run
through small bridge scripts in `~/.local/share/vtaiagent/bin/`. This cleanly
isolates GUI dependencies from the agent runtime.

The agent reads the desktop but does not click or type yet — acting safely
under Wayland means driving AT-SPI actions on found elements.

---

## Phase 7 — Absence mode *(deleted 0.3.1)*

**Delivered:** `notify`, `schedule_job`, the scheduler daemon, and systemd
integration.

- **APScheduler** hosts the loop in-process; jobs persist in SQLite so they
  survive restarts and reboots.
- Each firing is one bounded orchestrator run. Transient failures self-heal
  next tick; after `escalate_after_fails` consecutive failures, the agent
  texts your phone instead of looping silently.
- **systemd user services** (`vtai-agent.service`, `vtai-agent-api.service`)
  with `loginctl enable-linger` — the stack survives logout.
- Kill switch is honored inside each scheduled run.

**Key decision — no ntfy SDK.** The Python `ntfy` package collides with the
push client of the same name; ntfy is just an HTTP POST, so that's what it
uses.

**Bugs found and fixed:**
- `DetachedInstanceError` hit **three times** in the scheduler — ORM objects
  read after their session closed. Fixed at the source: all DB reads happen
  inside `session_scope`, and only plain dicts cross the boundary.

---

## Phase 8 — GUIs *(deleted 0.3.1)*

**Delivered:** FastAPI control plane, web dashboard, PyQt6 tray app.

- **FastAPI**: `/api/status`, `/api/jobs` (CRUD + fire), `/api/goal`,
  `/api/runs` (history + steps + audit), `/api/tools`, and a `/ws/logs`
  WebSocket for live updates.
- **Dashboard**: single self-contained HTML file — no build step, no npm.
- **Tray app**: opens the dashboard, toggles the daemon, sets the kill switch,
  polls health so the icon color reflects reality.

**Key decision — localhost-only.** The dashboard binds `127.0.0.1` because
`/api/goal` can execute actions. Remote access should go through an SSH tunnel
or TLS reverse proxy, never bare exposure.

**Bugs found and fixed:**
- `/api/status` called `registry.get()` without registering tools first —
  tools are now ensured at the top of every handler.
- `pkill -f "vtai api"` killed the invoking shell itself because the pattern
  matched the shell wrapper — the same class of imprecise-cleanup bug as
  Phase 5.

---

## Phase 9 — Security hardening for unattended use (0.3.1)

**Delivered:** fail-closed shell sandbox, repaired test/CI story, dead-code purge.

- A review found the shell sandbox was **advisory**: only `argv[0]` was checked,
  so `cat ~/.ssh/id_rsa` ran happily. `check_shell` now requires: bare name →
  allowlist → explicit sandbox mode (`none`/`read`/`write`, extendable via
  `shell_extra_modes`) → resolution inside `trusted_bin_dirs` → no exec-enabling
  flags → every path argument through deny/writable checks, resolved against
  the command's actual working dir.
- Deny globs match **resolved** paths, so `../` and symlink escapes no longer
  sidestep them, and `~/.ssh` itself (not just children) is protected.
- Children no longer inherit API keys/tokens: minimal env (PATH, HOME, LANG…).
- Dry runs truly preview: `backup_sync` doesn't mkdir, `delegate_coding`
  doesn't stage/snapshot; a failed snapshot now refuses to delegate instead of
  running without a rollback point. Rollback uses `git reset --hard` and audits
  failures.
- The kill switch is enforced by every executing tool, not just the orchestrator.
- 30 tests cover the bypass attempts that used to work; CI (uv sync --dev,
  pytest, ruff) actually runs.

**Deleted:** `api.py`, `scheduler.py`, `tray.py`, `verify.py`, `frontend/`,
`web/`, and the seven unregistered tools (`git`, `process`, `workflow`,
`notify`, `schedule`, `desktop`, `perception`).

**Key decision — fail-closed beats clever.** A per-command allowlist without a
per-command *policy* is theater: `find -exec`, `tar --checkpoint-action`, and
`rsync --rsync-path` are all "allowlisted binaries" doing arbitrary execution.
Unknown mode = denied.

## System changes summary

Everything installed or changed outside the project directory:

| Change | Location | Reversible via |
|---|---|---|
| pi provider repointed to Ollama | `~/.pi/agent/models.json` | `~/.pi/agent/models.json.bak` |
| pi default model updated | `~/.pi/agent/settings.json` | restore backup |
| Scheduler service | `~/.config/systemd/user/vtai-agent.service` | `systemctl --user disable` |
| API service | `~/.config/systemd/user/vtai-agent-api.service` | `systemctl --user disable` |
| Linger enabled | `loginctl` for your user | `loginctl disable-linger` |
| MCP registration | opencode + Claude config | `claude mcp remove vtai-agent` |
| AT-SPI + screenshot bridges | `~/.local/share/vtaiagent/bin/` | delete directory |
| Agent state + audit DB | `~/.vtaiagent/` | delete directory |

## Verification record

Every phase was tested end-to-end before moving on:

- **Phase 1** — 8 files organized; `rm` denied; MCP client handshake listed
  all tools.
- **Phase 2** — agent chained `system_info` → `organize_files` → final answer
  unaided; kill switch aborted a run in 0 steps.
- **Phase 3** — backup verified with 0 checksum differences; GPU transcode
  produced valid h264; re-encode loop protection found 0 jobs.
- **Phase 5** — opencode fixed a bug, verification passed, change kept;
  negative test rolled back a failing edit automatically.
- **Phase 6** — vision model correctly identified open applications; AT-SPI
  listed 8 real GUI apps; agent cross-referenced both sources.
- **Phase 7** — job fired and recorded `last=ok`; daemon reloaded persisted
  jobs after restart; escalation fired after 2 consecutive failures.
- **Phase 8** — dashboard HTTP 200; goal via API returned `status: ok`;
  WebSocket delivered live events; tray reported "API up / Daemon active".
