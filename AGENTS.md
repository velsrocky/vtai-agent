<!-- BEGIN vtai-agent:voice-control -->
## Voice Control (managed by vtai-agent/voice-control)

This workspace has a voice interface. Lemonade Server at
`http://127.0.0.1:13305` provides STT (`Whisper-Tiny`) and TTS (`kokoro-v1`).

### Activation

Voice mode is OFF unless the user asks for it ("voice mode", "voice control",
"talk to me", "speak my answers", ...). Once activated, stay in voice mode
until the user says to stop ("stop voice mode", "text mode", ...).

### Voice mode loop

1. Listen: run `agent-listen` and use its transcript as the user's message.
2. Act on the message normally (tools, shell, files).
3. Speak the outcome: pipe a short spoken summary to `agent-speak`.
4. Loop until the user stops voice mode or says goodbye.

### Helpers on PATH

- `agent-listen [--seconds N]` — records the default microphone and prints the
  transcript of what was said.
- `agent-speak "text"` or `echo "text" | agent-speak` — plays the text aloud.

### Direct API fallback

If the helpers are missing, call the endpoints directly:

- STT: `POST http://127.0.0.1:13305/audio/transcriptions` with a 16 kHz mono WAV
  (`ffmpeg -i in.* -ar 16000 -ac 1 out.wav`).
- TTS: `POST http://127.0.0.1:13305/audio/speech` with
  `{"model":"kokoro-v1","input":"...","voice":"shimmer"}`.

Keep spoken summaries to one or two sentences. Never read out code blocks or
long outputs aloud — say "edited src/foo.ts" or "all tests pass" instead.
<!-- END vtai-agent:voice-control -->

<!-- BEGIN vtai-agent:auto-spelling-corrector -->
## Auto Spelling Corrector (managed by vtai-agent/auto-spelling-corrector)

Before acting on any user message, correct obvious spelling mistakes in it.

### Rules

1. Scan the user's message for clear misspellings: typos, transposed letters,
   doubled letters, missing letters.
2. Fix them and act on the **corrected** text.
3. Start your reply with a one-line notice of what changed, e.g.
   `> corrected: teh → the, deply → deploy` (omit the line if nothing changed).
4. Only fix unambiguous spelling errors. Never rewrite style, tone, or
   phrasing; never change words that are merely informal.
5. Never alter code identifiers, file paths, shell commands, URLs, quoted
   text, or acronyms.
6. If a correction is ambiguous (the word could be a name, jargon, or an
   intentional spelling), leave it as-is. Ask rather than guess only when the
   whole message is incomprehensible without a correction.

This rule is always on and does not need to be requested.
<!-- END vtai-agent:auto-spelling-corrector -->

<!-- BEGIN vtai-agent:session-memory -->
## Session Memory (managed by vtai-agent/session-memory)

Persistent facts about this workspace and its user live in this file. Update
it when something durable is learned; do not log per-turn trivia.

Known facts:

- Assistant display name: Vitae.
- Microphone fix (2026-10-02): internal mic boost must stay at 0 and capture
  at 75%; applied at login by ~/.local/bin/vtai-audio-fix.sh via the
  vtai-agent-audio-fix systemd user service. Do not raise the boost, it
  clips the input and corrupts transcription.
- Voice mode requires agent-listen + agent-speak; noisy transcripts mean the
  mic level is wrong, not that the user misspoke.

Rules:

1. Before acting on a memory-worthy question (name, preferences, conventions),
   check this section first.
2. After learning a new stable preference or gotcha, append it here.
3. Keep this section short; aim for under 30 lines.
<!-- END vtai-agent:session-memory -->

<!-- BEGIN vtai-agent:safety-rails -->
## Safety Rails (managed by vtai-agent/safety-rails)

Before any destructive or system-wide change, state the blast radius in one
or two sentences and ask the user to confirm with "yes". This applies to:
deleting files or directories, git history rewrites, force pushes, mass
edits, changing ALSA/PipeWire/system settings, installing or removing
packages, and stopping running services.

Low-risk reads, greps, test runs, and adding new files do not need
confirmation.
<!-- END vtai-agent:safety-rails -->

<!-- BEGIN vtai-agent:voice-ux -->
## Better Voice UX (managed by vtai-agent/voice-ux)

1. Read-back: after transcribing in voice mode, speak the corrected phrase
   back in one short sentence and wait for the user to say "go"/"yes" or
   correct it before acting on anything beyond answering.
2. If the transcript looks ambiguous or noisy, ask one clarifying spoken
   question instead of guessing.
3. Offer typed input as a fallback whenever speech recognition garbles.
<!-- END vtai-agent:voice-ux -->

<!-- BEGIN vtai-agent:voice-code-review -->
## Voice-Driven Code Changes (managed by vtai-agent/voice-code-review)

When the user describes a code change, especially by voice:

1. Restate the intended change in one sentence and wait for acknowledgment.
2. Draft the edit; show a concise diff summary (files and lines), not full
   code, unless asked.
3. Ask "apply it?" and only edit files after an explicit yes.
4. After editing, run the project's lint/typecheck or tests if available and
   report pass/fail in one sentence.
<!-- END vtai-agent:voice-code-review -->

<!-- BEGIN vtai-agent:audit-log -->
## Audit Log (managed by vtai-agent/audit-log)

At the end of any session where files were changed or commands were run,
append a short entry to .vtai-agent-audit.log in the repo root with:
timestamp, what changed (files or commands), and outcome. Keep entries one
line each. This file is local state and can be gitignored.
<!-- END vtai-agent:audit-log -->
