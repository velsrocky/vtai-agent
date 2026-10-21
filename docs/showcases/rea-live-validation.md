# Showcase: the wrapper was live-validated, not just unit-tested

**Product layer:** `inspect_application` (REA capability) · **Status:**
live-validated 2026-10-21, pre-0.6.7 · **Evidence:** redundancy-free
audit rows below, `sha256:b210c5fd234c2913` / `ev_57f3794c…`

## The claim

`inspect_application` was unit-tested against a fake `rea` binary that
emits predetermined JSON. A fake proves our guardrails; it cannot prove
the wrapper works with the real thing — the real CLI's flag vocabulary,
its output envelope, and the evidence-id extraction were assumptions.

## What was measured against reality

REA (`rea-agents@6.7.0`, npm provenance: repository
`github.com/morluto/rea`, maintainer `morluto`) was installed and the
tool was driven end-to-end on a real JavaScript app (a two-file CSV
utility built for the purpose):

1. **Interface:** `rea analyze-javascript-application --help` lists the
   same subcommand vtai-agent's `command_prefix` configures — the
   configured spawn is the real spawn.
2. **Flag:** the real CLI accepts `--json` and emits a single
   parseable JSON Evidence document (`evidence_id` in top keys; the
   full output is a jsrg semantic graph with relations, function
   fingerprints, unknowns, and coverage families).
3. **End-to-end:** the tool quarantined the target (3 entries copied;
   original untouched), ran the resolved binary `rea
   analyze-javascript-application <copy> --json` under the minimal
   child environment, parsed the report, surfaced the real evidence id
   `ev_57f3794cd785debb05a4e08cb31a439df604abe2df4962e50b8edbd9e913343b`
   with digest `sha256:b210c5fd234c2913`, wrote both audit rows
   (quarantine + evidence), and deleted the quarantine copy.

Per-machine audit trail (real rows, action `rea`):

```text
allowed=True | quarantined 3 entries of 'rea-target' into .../quarantine/r-abc599dd4af8
allowed=True | REA exited 0; evidence sha256:b210c5fd234c2913
```

## Side findings

- **frida's blocked postinstall script did not matter** for this path:
  REA's JS static analysis ran clean (exit 0) on an npm install whose
  lifecycle scripts were policy-blocked. If a REA subcommand ever needs
  the frida native binding, the tool fails closed with REA's own
  nonzero exit plus stderr in the result detail — not silently.
- The capability was enabled only in-process for the validation run;
  no config file on this machine is changed, and the capability
  returned to `enabled = false` after the run.
