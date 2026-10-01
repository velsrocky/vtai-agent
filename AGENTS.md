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
