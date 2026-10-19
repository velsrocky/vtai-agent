# Usage Examples

All examples use the eight registered tools: `system_info`, `run_shell`,
`organize_files`, `backup_sync`, `transcode_media`, `disk_audit`,
`delegate_coding`, `auto_mode`. Anything not allowlisted is denied — and
audited.

## Goals (orchestrated, natural language)

```bash
uv run vtai goal "Organize my Downloads folder" --dry-run
uv run vtai goal "Organize my Downloads folder" --execute
uv run vtai goal "Find the biggest reclaimable files on this machine"
uv run vtai goal "Back up ~/Documents/vt-data to ~/Documents/vt-data-backup" --execute
```

## Direct tool calls (no LLM involved)

```bash
# System snapshot
uv run vtai run system_info

# Disk audit: large, old files under a path (read-only, deletes nothing)
uv run vtai run disk_audit --params '{"path":"~/Downloads","min_size_mb":100,"older_than_days":90}'

# Preview a backup, then execute it
uv run vtai run backup_sync --params '{"source":"~/Documents/vt-data","destination":"~/Documents/vt-data-backup","dry_run":true}'
uv run vtai run backup_sync --params '{"source":"~/Documents/vt-data","destination":"~/Documents/vt-data-backup","dry_run":false,"verify":true}'

# Transcode with the AMD GPU (originals never modified)
uv run vtai run transcode_media --params '{"directory":"~/Downloads/clips","codec":"hevc_vaapi","dry_run":true}'

# Allowlisted shell, dry-run first
uv run vtai run run_shell --params '{"command":"du -sh ~/Downloads/*","dry_run":true}'
uv run vtai run run_shell --params '{"command":"du -sh ~/Downloads/*","dry_run":false}'
```

## Delegate a coding task

```bash
uv run vtai run delegate_coding --params '{
  "cli": "opencode",
  "prompt": "Fix the off-by-one in parser.py",
  "working_dir": "~/projects/myapp",
  "verify_command": "pytest",
  "auto_rollback": true,
  "dry_run": true
}'
```

The working dir must be a git repo inside `writable_roots`. It is snapshotted
before the delegate runs; a failed verification rolls the repo back and the
rollback result lands in the audit log. Verify commands go through the same
sandbox as `run_shell` — `pytest` is allowlisted out of the box; anything else
needs an entry in `shell_allowlist` + `shell_extra_modes`.

`require_approval` defaults to `true`:

- **Interactive** runs show `git diff --stat` and ask before keeping changes.
- **Non-interactive** runs (MCP, scripted, orchestrated) keep the changes in
  the worktree and record a *pending approval*; the run reports failure
  ("changes kept for review") so automated loops stop. Resolve it with
  `vtai approve <id>` (keep) or `vtai approve <id> --rollback` (discard).
  While a pending approval exists for a repo, `delegate_coding` refuses new
  work there — even with `require_approval: false`.

Pass `"require_approval": false` only for tasks a human has already reviewed.
Also note `cli: "pi"` only works when pi has an authenticated provider
(`pi auth check`); otherwise it returns a failure complaining the delegate
applied no changes.

```bash
uv run vtai approve              # list pending approvals
uv run vtai approve 7            # keep the pending diff
uv run vtai approve 7 --rollback  # discard it
```

## Auto mode (goal in, reviewed iterations)

```bash
uv run vtai run auto_mode --params '{
  "goal": "add a unit test for parser.py",
  "working_dir": "~/projects/myapp",
  "cli": "opencode",
  "max_iterations": 3,
  "dry_run": true
}'
```

A coding CLI gets the goal, a judge model reviews the reply against the
goal, and an insufficient reply triggers a corrective follow-up — up to
`max_iterations`. The judge accepts only a bare `OK` as satisfied. Spend is
metered against `guardrails.budget_usd_per_run`: the judge's calls always,
the delegate CLI when it reports it (`claude -p --output-format json` exposes
`total_cost_usd`; `opencode`/`pi` report no headless cost and are labelled
`"cli_usd": "unmeasured"`).

From inside opencode or Claude Code, start your prompt with
`@vt-ai_automode_on` and the MCP server routes it to this tool.

## What gets denied

```bash
uv run vtai run run_shell --params '{"command":"rm -rf /tmp/vtaiagent"}'
# binary 'rm' not in allowlist

uv run vtai run run_shell --params '{"command":"cat ~/.ssh/id_rsa"}'
# argument rejected: Protected path: ~/.ssh/id_rsa

uv run vtai run run_shell --params '{"command":"cp ~/secrets.txt /etc/"}'
# argument rejected: not writable (outside writable_roots)

uv run vtai run run_shell --params '{"command":"rsync -e bash a b"}'
# flag '-e' not permitted for 'rsync'
```

## Kill switch

```bash
touch ~/.vtaiagent/KILL    # aborts current and refuses new executing tool runs
rm ~/.vtaiagent/KILL       # resume
```

## Audit everything

```bash
sqlite3 ~/.vtaiagent/vtai.db "select created_at, action, allowed, detail
  from auditlog order by id desc limit 20;"
```
