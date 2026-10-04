# Changelog

## [Unreleased]

## [0.5.2] - 2026-10-04

### Added
- **`auto_mode` tool**: the agent loops a coding CLI (opencode/claude/pi),
  reviews its reply against the goal with the configured model, and
  re-prompts with the critique until the answer satisfies the goal or
  `max_iterations`. Trigger it from opencode/claude prompts that start with
  `@vt-ai_automode_on` — the MCP server's instructions route those to the
  tool. Dry-run supported; each iteration is audited.

## [0.5.1] - 2026-10-04

### Fixed
- **Global installs find their config.** `CONFIG_TOML` now resolves
  `$VT_CONFIG` → repo-adjacent file → `~/.vtaiagent/settings.toml`, so a
  `uv tool`-installed `vtai` no longer falls back to defaults (empty
  allowlist, wrong model) outside the repo.
- **Version comes from package metadata** (single source: pyproject), so
  `vtai info` can't drift from the installed release again.

## [0.5.0] - 2026-10-03

### Added
- **HTTP transport for the MCP server**: `vtai serve --transport http
  --host --port --token` exposes the same guarded tools over StreamableHTTP.
  Bearer-token auth via `--token`/`VT_HTTP_TOKEN`; binding a non-localhost
  address without a token is refused. Localhost without a token remains
  allowed for single-operator use.
- **DB schema versioning**: a `vtai_metadata` table records the schema
  version; `init_db` refuses DBs from newer binaries and leaves a clearly
  marked place for future migrations.
- **macOS/Windows CI matrix**: tests now run on ubuntu + macos (blocking)
  and windows (experimental, continue-on-error).
- **`vtai runs`**: browse run history (id, started, status, dry/exec,
  provider, step count, cost, goal) with `--limit`, `--status`, `--grep`.
  `finish_run` now records step counts on the run row.
- **Docker hardening**: container now runs as a non-root `vtai` user
  (uid 1000, `HOME=/home/vtai`), with the writable roots and cargo bin dir
  pre-created, a `HEALTHCHECK` via `vtai info`, and compose volumes mapped
  to `/home/vtai` instead of `/root`.
- **`vtai init`**: writes a starter `config/settings.toml` when missing
  (`--force` regenerates), ensures data dir and writable roots exist, then
  validates the result. Prints the next two commands to try.

### Fixed
- **Budget accounting fails closed.** `providers.estimate_cost` now returns
  `None` when a paid provider's spend can't be determined (no explicit
  pricing config and no native cost) instead of silently treating it as
  free; the orchestrator aborts with a clear message. Local providers
  explicitly cost 0.
- **Trusted-binary check uses real path containment.** `str.startswith` on
  the resolved binary path would accept a sibling prefix like
  `/usr/bin.evil`; it now requires `is_relative_to` a trusted bin dir.
- **`run_shell` validates path args against the actual execution cwd.**
  `check_shell` receives `base_dir=cwd` and the child process is spawned
  with that cwd, closing the asymmetry with `delegate_coding`.

### Tests
- 11 new tests: `organize_files` (dry-run, sort, collision rename), delegate
  rollback-on-failed-verify and keep-on-pass, media job enumeration, and
  orchestrator loop runs (tool-call round-trip via a scripted FunctionModel,
  kill switch, unknown-paid-cost fail-closed). 58 tests pass.
- Attempted a native function-calling rewrite of the orchestrator; verified
  on 2026-10-03 that the local Ollama models (qwen2.5-coder:7b) emit tool
  calls as raw text instead of structured calls, so the text protocol stays.

### Fixed (earlier in this cycle)
- **Shell sandbox: attached path-flag values bypassed writable/deny checks.**
  `tar -C/etc`, `unzip -d/etc`, `cp --target-directory=/etc`, `mv -t/etc`, and
  `grep -f /etc/shadow` (attached or `--opt=value` form) passed an option
  starting with `-` straight past the path checks. `SHELL_PATH_FLAGS` now
  extracts these values into the checked path args. Verified with live repro
  before the fix.
- **`delegate_coding` diff capture missed new files.** `git diff --stat` never
  reports untracked files, so a delegate that only *added* files looked like
  "no changes" (which could also trip the pi fail-closed heuristic). The tool
  now lists untracked files explicitly, and emptiness detection is based on
  changed tracked files plus untracked ones.
- **Delegate no longer leaves your index fully staged.** The staging done for
  snapshot/diff is reverted: the user's original index tree is captured
  (`git write-tree`) before `git add -A` and restored (`git read-tree`) on
  every exit path, rollback included.
- **`delegate_coding` via MCP can set `require_approval`.** The MCP wrapper
  omitted the parameter, so it always defaulted to `true` and always rolled
  back (stdio is not a TTY, approval can never happen). The parameter is now
  exposed; default behavior unchanged.
- **Recursive `organize_files` no longer "duplicates" already-organized
  files.** Files already inside their category directory (e.g. from an
  earlier run) were re-planned and renamed to `name_1.ext`. They are now
  skipped.

### Tests
- 2 new regression tests (attached path flags, recursive organize duplicate);
  50 tests pass.

## [0.4.0] - 2026-10-02

### Added
- **`vtai audit`**: list and filter audit-log entries by `--limit`, `--action`,
  `--run-id`, `--allowed`/`--denied`, and `--grep` (substring on detail).
- **`agent-listen --vad`**: silence-based turn detection — records until 1.5s of
  quiet after speech, instead of always using the fixed `--seconds` window.
- **`delegate_coding.require_approval`** (default `true`): after the delegate
  runs and optional `verify_command` passes, the tool shows `git diff --stat`
  and requires explicit human approval before keeping changes. Non-interactive
  runs roll back and report `needs_approval` instead of silently keeping edits.
- **`validate_settings()` at startup**: fails fast on an empty
  `shell_allowlist`, nonexistent `trusted_bin_dirs`/`writable_roots`, invalid
  `deny_globs` patterns, or `shell_extra_modes` keys not in the allowlist.
- **Audit-log doc + session memory** blocks in `AGENTS.md`.

### Fixed
- **`guardrails.is_denied`**: a path that cannot be resolved (e.g. embedded null
  byte raising `ValueError`) is now treated as denied instead of crashing.
- **`agent-listen` / `agent-speak` hardening**: `--seconds` is validated,
  STT/TTS requests are retried (3x), and failures exit non-zero with clear
  reasons — 3 recorder failed, 4 STT/TTS unreachable, 5 STT bad JSON,
  6 no speech detected. Long transcripts (probable garbage) are flagged.
- **`delegate_coding` with `pi`**: when `pi` has no authenticated provider it
  exits 0 while emitting a raw tool-call JSON block and applying nothing. The
  tool now detects that shape and fails closed (``"did not apply any changes"``)
  with a pointer to `pi auth check`. Prefer `opencode` or `claude` until `pi`
  is authenticated on this machine.
- **Voice audio on ALC294**: internal/headset mic boost pinned at max clipped
  input; a systemd user service (`vtai-agent-audio-fix`) now resets
  `Internal Mic Boost` and `Headset Mic Boost` to 0 and `Capture` to 75% at
  every login, documented in `README.md` troubleshooting.

### Tests
- 8 new tests in `tests/test_guardrails.py` (denial auditing, kill-switch
  audit, symlink-into-protected escape, unresolvable paths, deny-glob exact
  dir) and 1 new `delegate_coding` fail-closed test; 60 tests pass total.

## [0.3.2] - 2026-09-26

### Added
- **Echo-loop circuit breaker**: after `guardrails.max_repeat_denials` (default
  3) denials of the identical action within `denial_window_seconds` (default
  300), the refusal message itself escalates with a `CIRCUIT BREAKER` note —
  visible to the looping model in both MCP and goal modes, and audited.
  In-process by design: it targets long-running sessions (opencode/`vtai goal`),
  the echo-loop scenario.

## [0.3.1] - 2026-09-26

### Fixed
- **run_shell sandbox**: was allowlisting only argv[0]; now the binary must be a
  bare name, resolve inside `guardrails.trusted_bin_dirs`, carry no exec-enabling
  flags (`rsync -e/--rsync-path`, `tar --checkpoint-action/...`, `zip --pipe`),
  and every path argument passes deny-glob/writable-roots checks (fail-closed
  per-command read/write policy). Children run with a scrubbed env (no API keys).
- **Deny-glob matching** now uses resolved paths, so `..`/symlink escapes and the
  protected directory itself (`~/.ssh`, not just children) are denied;
  `disk_audit` filters deny-listed dirs out of scans and no longer measures them.
- **Dry-run side effects**: `backup_sync` no longer mkdirs the destination in a
  dry run; `delegate_coding` no longer stages/snapshots the repo (`git add -A`
  before the dry-run return removed) and refuses to run without a valid snapshot;
  rollback now uses `git reset --hard` and audits failures instead of swallowing them.
- **Kill switch** (`data_dir/KILL`) is now checked by every executing tool, not
  only the orchestrator.
- **Verify commands**: `guardrails.shell_extra_modes` lets config extend the
  fail-closed policy to new binaries (`pytest = "write"` shipped); relative
  path arguments are resolved against the command's working dir, so delegate
  verification commands are actually checkable.
- **Tests**: `Guardrails(cfg, settings)` injects settings instead of reading
  globals; test suite rewritten (38 tests: sandbox bypass attempts, tool
  dry-run/execute behavior, env scrubbing). CI runs `uv sync --dev` with dev
  deps declared; ruff clean across src/ and tests/.
- **Budget enforcement**: `guardrails.budget_usd_per_run` is now actually
  enforced in the orchestrator — per-step token cost (explicit
  `cost_per_1k_input/output` config, else pydantic-ai's native cost table,
  else free) accumulates and aborts with status `budget_exceeded`; totals are
  persisted on the run row.
- **Audit attribution**: direct MCP and CLI tool invocations now create their
  own Run rows (`tool:<name>`, provider `mcp`/`cli`) with final status
  ok/failed/denied, and guardrail denials attach to that run via a context
  var instead of landing as `run_id NULL`.
- **Orphan sweep**: startup marks runs left in `running` by dead processes as
  `failed` (audited, idempotent) so history reflects reality.

### Removed
- Dead/broken modules deleted: `api.py`, `scheduler.py`, `tray.py`, `verify.py`,
  the React `frontend/` + `web/` dashboard, and unregistered tools
  (`git`, `process`, `workflow`, `notify`, `schedule`, `desktop`, `perception`).

## [0.3.0] - 2026-09-18

### Added
- **Stability**: Tool routing, caching (disk_audit, analyze_screen), retry logic
- **Enterprise**: SSO/OIDC authentication, RBAC, audit export
- **Observability**: Prometheus `/metrics` endpoint, `/health` endpoint
- **Fleet**: Agent heartbeat and multi-node registration
- **CLI**: `vtai daemon` background service mode
- **Tools**: Git operations, process management, desktop interaction (AT-SPI)

### Changed
- Updated `pyproject.toml` dependencies (authlib, prometheus-client, jinja2, starlette)
- Refactored `api.py` with session-based auth
- Improved `system_info` tool with CPU name detection

### Fixed
- Duplicate tool registration issues
- Test suite stability (guardrails tests)

---

## [0.2.0] - 2026-09-17

### Added
- Initial MCP server integration
- Workflow DSL support
- AT-SPI bridge for UI interaction

---

## [0.1.0] - 2026-09-16

### Added
- Initial release: CLI daemon, web dashboard, core automation tools
