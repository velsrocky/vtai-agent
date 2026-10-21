# Showcase: voice mode, tested against a hostile microphone

**Product layer:** voice control (`agent-listen` / `agent-speak`,
`vtai voice-sync`) · **Shipped:** v0.5.5 era · **Evidence:**
`scripts/agent-listen`, session discipline notes, CHANGELOG

## The problem nobody demos

Voice input fails long before speech recognition does. Voice mode shipped
in v0.5.3 with a listen helper, and the first real sessions exposed a
chain of non-model failures that no amount of ASR quality fixes:

1. **Mic boost double-gain.** The internal mic had boost + capture both
   raised; the waveform clipped, and `Whisper` transcribed the clipping
   noise into confidently wrong text. Noisy transcripts look like "bad
   model", but the fix was level discipline: **boost stays at 0, capture
   at 75%**, enforced at login by an audio-fix script.
2. **Hangs instead of endings.** The listener used to wait forever after
   the sentence ended. `agent-listen --vad` stops on 1.5 s of measured
   silence instead of a fixed timeout, so round-trips feel conversational.
3. **Failures you can't see.** Bare-exit helpers are undebuggable at
   4 a.m. Every failure mode now has a code and a sentence on stderr:
   `exit 3` = recording failed (mic not capturing), `exit 4` = STT server
   unreachable, `exit 6` = no speech detected — *with the actual
   interpretation* ("check mic input").
4. **Repeatability.** `vtai voice-sync` installs both helpers from the
   versioned `scripts/` dir to `~/.local/bin` with checksums and
   `--check` drift reporting — what runs on your machine is what's in
   the repo, not an older copy from whenever.

## The UX contract

The read-back loop is deliberate: after transcribing, the assistant speaks
the corrected phrase back **and waits** for "go" (or a correction) before
acting on anything more than answering — plus a typed input fallback
whenever recognition garbles. A voice interface you have to trust blindly
is a toy; one that verifies before acting is an operator's tool.

## Why this is a differentiator

Agent competitors measure tokens per second on a benchmark. Under real
desk conditions — mic clipping, servers down, half-finished sentences —
the winner is the one whose failure modes were *observed in actual use*
and closed with measurable, re-runnable contracts. Every rule in this
slice came from a session that broke, not a design doc.

## How you verify it yourself

```bash
sed -n '80,90p'  scripts/agent-listen   # the 1.5 s silence stop
sed -n '150,190p' scripts/agent-listen  # exit codes 3/4/6 with reasons
uv run vtai voice-sync --check          # installed helpers match repo checksums
```
