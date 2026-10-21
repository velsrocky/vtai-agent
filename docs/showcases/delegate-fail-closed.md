# Showcase: the CLI that said it succeeded when it hadn't

**Product layer:** `delegate_coding` · **Shipped:** v0.5.3
(`afd6184`, 2026-10-13) · **Evidence:** code, tests, audit rows

## The failure in the wild

During a real working session (2026-10-02) a task was delegated to `pi`.
The CLI exited **0** and printed a plausible-looking blob — so a success
path built on "returncode == 0" would have told the orchestrator *"the
edit is in."* Except there was no edit: `pi` had no authenticated provider
that day and answered with its own raw tool-call JSON, the moral
equivalent of writing the task into chat instead of the repository.

Files `app.py`s appear in host repos the same way (`6c0f46d` removed a
stray `app.py` that a mid-fix delegate run wrote into the host repo). A
delegate that lies softly is *worse* than one that crashes: nobody
investigates an exit code of zero.

## The fix: fail closed on a phantom edit

`src/vtai_agent/tools/delegate.py` now treats "exit 0 and no diff" itself
as a failure. The rule that a delegate's newest files are **untracked** is
handled next to it, so an add-only change cannot be mistaken for "no
changes" either:

```python
# A delegate that "succeeds" but produced no changes is not a success:
# pi emits a raw tool-call JSON when it has no authenticated provider,
# and an unchanged tree can also mean the CLI silently did nothing.
# Fail closed either way so callers do not proceed on a phantom edit.
if code == 0 and not diff.strip():
    ... "delegate proposed a tool call but never executed it
         (check pi auth/session; pi returns this when
          no provider is authenticated)" ...
```

The same commit repaired the `opencode` invocation (`run` takes the
message positionally; `--dir` was never a flag) — the ghost of a *different*
silent failure, where the wrong argv would delegate into the wrong repo.

## Why this is a differentiator

Any agent harness can paste a prompt into a coding CLI. The hard part is
the **contract**: before another tool consumes the "success," the tree must
have actually changed, the change must pass a shadowed verification
command, and `auto_rollback` restores the snapshot if it doesn't. The
system-automation guard (fast to demo) is certification-grade
truth-attribution (slow to build) — the exact trade REA-scale tools skip.

## How you verify it yourself

```bash
gh api repos/velsrocky/vtai-agent/commits/afd6184   # the fix
sed -n '180,199p' src/vtai_agent/tools/delegate.py  # the phantom-edit gate
git show 6c0f46d                                    # the stray app.py
```
