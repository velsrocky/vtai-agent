# Security Policy

VT-AIAgent runs commands and moves files on your machine. It is designed so
that a mistake — by you or by a model it is driving — is contained rather
than destructive. This document says what that means, and how to report a
problem with it.

## Security model in one paragraph

Every tool call passes through the same guardrails before anything happens:
writes are confined to `paths.writable_roots`, reads and writes matching
`guardrails.deny_globs` are refused, shell commands are restricted to
allowlisted binaries that resolve inside `guardrails.trusted_bin_dirs` and
carry no exec-enabling flags, and actions run dry by default. Every allow
and deny is written to an audit log.

## What is in scope

- A guardrail that can be bypassed — a path that should have been denied but
  was not, a binary that should have been refused, a dry run that touched
  disk.
- Anything that leaks the contents of a denied path (secrets in
  `~/.ssh`, `~/.gnupg`, `~/.aws`, `**/.env`).
- A way to make the agent execute a command that was never allowlisted.
- The HTTP transport binding or serving without the bearer token it
  requires, or serving to a remote host without one.

## What is out of scope

- A model choosing a destructive-but-allowlisted action. `rm` is not
  allowlisted; `mv` inside a writable root is, by design.
- Misconfiguration that widens the sandbox on purpose (`writable_roots`,
  `deny_globs`, `trusted_bin_dirs` are the user's own settings).
- Running the agent against a directory you do not control.
- Denial of service against a local endpoint you exposed yourself.

## Reporting a vulnerability

Please report privately rather than in a public issue: use GitHub's
**Security → Report a vulnerability** button on this repository, or email the
maintainer. Include:

- what the guardrail should have refused and what happened instead,
- the config that reproduces it (the relevant `writable_roots`,
  `deny_globs`, `trusted_bin_dirs`, `shell_allowlist`),
- the agent version (`vtai --version`) and OS.

You should get an acknowledgement within a few days. Fixes ship as a
patch release, and the advisory is published with the release notes.

## Supported versions

The latest released version receives security fixes. Older tags are not
maintained — please upgrade (`uv sync` and re-run
`scripts/register-mcp.sh` after pulling).

## Hardening notes for operators

- Keep `dry_run_default = true` until you trust the tool.
- Keep `deny_globs` covering every secrets directory on the machine, and
  never set it to an empty list.
- Narrow `writable_roots` to what the agent actually needs.
- Remember the kill switch: `touch ~/.vtaiagent/KILL` stops any executing
  tool; remove the file to resume.
- For the HTTP transport, always pass `--token` (or set `VT_HTTP_TOKEN`) —
  the server refuses to bind a non-localhost address without one, but a
  token on a shared network still needs to be a real secret.
- `vtai audit --denied` shows what was refused, including refusals you did
  not expect.
