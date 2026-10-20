## What this changes

<!-- One or two sentences: the behaviour that changes, not the file list. -->

## Why

<!-- The problem this solves. Link the issue if there is one. -->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check src/ tests/` passes
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`
- [ ] New behaviour is covered by a test (and the test fails without the change)
- [ ] If a shell binary was added: it is in `guardrails.shell_allowlist`,
      has a mode in `guardrails.py` / `shell_extra_modes`, and resolves
      inside `trusted_bin_dirs` — `run_shell` is fail-closed
- [ ] The change is safe by default: `dry_run_default` stays `true` unless
      the change is explicitly about that
- [ ] No new path escapes `writable_roots`, and no new way to read a
      `deny_globs` path was introduced
- [ ] Docs updated (README / docs/) if the user-visible behaviour changed

## Security

<!-- If this touches the guardrails, the sandbox, the HTTP transport, or the
     delegate workflow, say so explicitly and explain the blast radius. -->

- [ ] No guardrail was weakened
- [ ] Audit logging still covers the new path

## Testing notes

<!-- How you verified it, including any platform you could not test. -->
