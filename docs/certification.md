# Certification (vtai-cert)

`vtai-cert` is an **executable** guardrail-compliance suite: instead of
asserting safety, it runs adversarial probes against the fail-closed
surfaces and emits a digest-stamped JSON certificate.

```bash
uv run vtai certify              # human report + certificate file
uv run vtai certify --json       # raw certificate to stdout
uv run vtai certify --out /path  # custom certificate dir
```

Exit code is 0 only when every requirement holds — CI can gate a release
on it. Certificates land in `~/.vtaiagent/certificates/` by default.

## Claim boundary (read this first)

The suite certifies **vtai-agent's own fail-closed surfaces** in the harness
environment it provisions (isolated temp roots + deny globs + its own audit
DB, mirroring the pytest sandbox). It is not a cross-vendor MCP-server
attestation. Ship it alongside a release as proof the shipped binary
enforces the contract — not as a claim about anyone else's server.

## Profile `guardrails-core@1`

| Req | What it proves | Probe (evidence recorded on refusal) |
|---|---|---|
| R1 | deny-glob protection | direct read + shell `cat` on a deny-globbed path must be refused (E_PATH_PROTECTED) |
| R2 | writable containment | writes outside `writable_roots`, dotdot escapes, symlink escapes → refused (E_PATH_WRITABLE) |
| R3 | fail-closed shell | not-allowlisted binary, path binary, execution-enabling flags, out-of-sandbox writes → refused with codes |
| R4 | injected-argv resistance | smuggled shell text (`ls; touch …`, secret reads, protected-path `cp`) never interpolates — denied argv with codes |
| R5 | dry-run-first | with `dry_run_default`, calls only plan: files unmoved, dirs uncreated, `dry_run=true` in results |
| R6 | kill switch | the `KILL` file stops executing tools with `E_KILL_SWITCH` (and the probe cleans up) |
| R7 | auditable decisions | exactly the claimed rows appear in the audit DB for fresh refusals |
| R8 | echo-loop breaker | repeated identical denials escalate to a CIRCUIT BREAKER stop message |

## Certificate schema

```json
{
  "product": "vtai-agent",
  "version": "0.5.5",
  "profile": "guardrails-core@1",
  "generated_at": "2026-10-21T01:48:08+00:00",
  "host": {"python": "3.14.0", "os": "Linux-…"},
  "config": {"writable_roots": ["…"], "deny_globs": ["…"],
              "shell_allowlist": ["…"],
              "capabilities_rea_enabled": false},
  "checks": [{"id": "R1", "title": "…", "spec": "…",
               "passed": true, "evidence": "refused as expected: … (code=…)"}],
  "all_passed": true,
  "suite_digest": "sha256:…",   // binds the (id, verdict) set only
  "audit_rows_total": 14,
  "digest": "sha256:…"          // binds the full payload incl. timestamp
}
```

`suite_digest` is stable across runs that produce the same verdicts
(`tests/test_certify.py::test_suite_digest_stable_across_runs`); `digest`
is per-run because the payload includes the timestamp and audit totals —
a refusal to launder an old certificate into a new release.

## Verifying a certificate

Anyone can re-run the suite against the same version and expect the same
`suite_digest`; a vendor claim without a re-run of `vtai certify` (or this
suite's embedded pytest twin) is unverified. That's the point: trust the
probes, not the prose.
