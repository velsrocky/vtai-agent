# Showcase: a certificate you can re-run, not a claim you must believe

**Product layer:** `vtai-cert` (`vtai certify`) · **Shipped:** v0.6.6 ·
**Evidence:** `suite_digest 914d9b81a7f35827` reproduced across runs and
across a code rebase; 3 real defects caught on the way

## Safety claims are normally prose

Every agent tool says "sandboxed" and "fail closed" in its README. The
regression suite is CI's opinion of the code as written by the author of
the code. `vtai-cert` is a different artifact: an **executable
compliance harness** that treats its own fail-closed surfaces as hostile
and records refusal evidence with stable error codes.

Eight requirements R1–R8 run against a freshly provisioned isolated
machine (temp roots, deny globs, private audit DB): deny-glob reads,
dotdot **and symlink** escape writes, not-allowlisted/path/exec-flag
shell exploitation, injected-argv smuggling (`ls; touch …`), dry-run
first, kill switch, audit-row exactness, and the echo-loop breaker that
escalates repeated identical denials into a `CIRCUIT BREAKER` stop.

## What the certification actually caught

Three real defects that 100+ existing tests never saw — found while the
harness was being built against it:

1. **Trusted-bin-dir seeding resolved the wrong thing.** The harness
   seeded trusted dirs from binary *names* (`Path("mkdir")`), not the
   `which()` result; the sandbox then trusted a directory that did not
   contain the actual resolved binaries — and probes caught binaries
   silently shifting policy (`E_SHELL_BIN_UNTRUSTED` for a seeded allow).
2. **The refusal model is asymmetric — and the harness got it wrong
   before it got it right.** `check_*` guards *raise*, but `check_shell`
   *returns* a denial decision. A probe written naively treats a returned
   refusal as "allowed." Encoding that asymmetry into the suite makes it
   impossible to confuse again.
3. **Global-state leakage.** The harness swapped `config._settings` and
   `db._engine` to isolate its audit DB — and would have contaminated any
   caller in the same process. Now snapshotted and restored; the
   certificate default-path bug it caused was visible before the fix.

Then, post-rebase on the evolved 0.6.5 guardrails: the probes' scratch
setup wasn't idempotent on an existing root — `R5` failed with a
`FileExistsError` on the *second* run. Reproducibility is part of the
spec, so the probes now reset their own state, and re-running is a
requirement, not an accident.

## The number that means something

```text
suite_digest sha256:914d9b81a7f35827; all_passed=True
```

Same digest **before** and **after** rebasing the suite onto a month of
someone else's guardrail evolution (0.6.3→0.6.5 added judge budgets,
spawn hardening, audit visibility). The verdict semantics survived
unchanged — that is the property the digest is for. Weaken any guardrail
and the suite fails closed (`tests/test_certify.py::
test_weakened_guardrails_fail_the_suite`); the per-run digest binds the
timestamped payload so an old certificate cannot be laundered into a new
release.

## Claim boundary — stated, not buried

This certifies vtai-agent's own enforced surfaces in the harness
environment. It is not a cross-vendor MCP attestation. The certificate is
the evidence a user can bake into a release; it is why the release claim
is "these probes passed" instead of "we promise it's safe."

## How you verify it yourself

```bash
uv run vtai certify                # full report + certificate file; exit 0 iff all hold
uv run pytest tests/test_certify.py -q      # the pytest twin
sed -n '/def probe_writable_containment/,/^def /p' src/vtai_agent/certify.py
```
