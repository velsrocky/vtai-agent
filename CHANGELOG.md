# Changelog

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
  globals; test suite rewritten (30 tests: sandbox bypass attempts, tool
  dry-run/execute behavior, env scrubbing). CI runs `uv sync --dev` with dev
  deps declared; ruff clean across src/ and tests/.

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
