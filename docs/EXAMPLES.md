# Usage Examples

All examples use the seven registered tools: `system_info`, `run_shell`,
`organize_files`, `backup_sync`, `transcode_media`, `disk_audit`,
`delegate_coding`. Anything not allowlisted is denied — and audited.

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
