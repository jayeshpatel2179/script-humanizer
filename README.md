# script-humanizer

A Telegram bot that **edits** a finished video script so it sounds human and
is ready for ElevenLabs. It doesn't generate scripts. You write the script in
Claude chat as usual; this bot cleans it up.

It works on any topic (FC 27 reviews, football previews, anything else). It
looks for language patterns, not topics.

## How it works: one Google Doc, three buttons

1. Paste the Claude script into the configured Google Doc (tab `t.0`).
2. Send the bot **any message**. It replies with a menu:
   **🧹 Go Humanize** · **🎭 Add Emotion** · **🔄 Cancel / New Script**.
   Typing `go humanize` runs Go Humanize directly. Typed text is never
   treated as a script.
3. **🧹 Go Humanize** cleans the wording only: it cuts repeated and filler
   words, varies repeated openers, rewrites "it's not X, it's Y" lines,
   simplifies hard words, and strips "genuine/genuinely" in code. It **adds no
   cues**, and cues already in the script are kept in order. It replaces the
   Doc and replies with the link, a report and two buttons:
   **🎭 Add Emotion** · **🔄 Cancel / New Script**.
4. **🎭 Add Emotion** (optional) takes the humanized script from the Doc,
   adds Eleven v4 audio tags such as `[thoughtful]`, writes it back, and
   replies with an emotion report. Then clear the Doc and paste the next
   script.
5. **🔄 Cancel / New Script** (or `/cancel`) closes the session. It never
   reads, writes or clears the Doc. If a job is running, it stops before
   the Doc write; once the write has happened, it's too late and the bot
   says so.

Every run ends as **one message**: the pressed menu (or a new message) turns
into "⏳ Processing...", which is then edited into the report or into
"Couldn't finish: <check>. The Doc was not changed." **No file is ever
sent.** Replies use Telegram HTML, and every dynamic string is escaped.

Editor notes (`[Editor note: ...]`), the `Short 1` / `Short 2` header lines
and the `Difficult to pronounce words` list are swapped out for placeholders
before either model call. They come back byte-for-byte and never get cues.
A raw copy of the Doc is saved to `STATE_DIR/backups/` (newest 3 kept)
before either step runs.

### Pre-flight checks (read-only, no model calls)

**Go Humanize**, in order:

1. Job already running → "⏳ Already working on a script…"
2. Doc tab empty → "📭 The Doc is empty…"
3. Text matches the last raw input, the last humanized output or the last
   emotion output (hash after collapsing whitespace) → "♻️ This is the same
   script I already processed…"
4. The last written text is still there with new text pasted below it →
   "⚠️ The Doc still has the old script…"
5. Fewer than `DOC_MIN_WORDS` words → "That's only N words…"

**Add Emotion**, in order: job running → empty → session cancelled → emotions
already added → not the script Go Humanize last wrote ("Run Go Humanize on
this script first") → already contains cues.

### Session state (`STATE_DIR/state.json`)

`last_input_hash`, `last_humanize_hash` and `last_emotion_hash` (hashes of
the Doc as re-read after each write), `last_output_hash`,
`last_written_text`, and `session_closed` (set by Cancel, cleared by the
next successful Go Humanize). Cancel keeps the hashes, so same-script
protection stays. On Railway this survives restarts only on a volume.

### Validation gates (code only, each step retried once)

| Step | Fails when |
|---|---|
| Go Humanize | a `Short 1`/`Short 2` header is missing, changed, out of order or empty; the "if you like(d) watching this" outro disappears; "Chapter <number>" is added; the word count changes more than `DOC_LENGTH_TOLERANCE` (±15%); the output was cut off; **the ordered cue sequence differs from the input's** |
| Add Emotion | **with every cue removed, the text differs from the input** (exact, after whitespace normalisation); a Short header, editor note or the pronunciation list changed or got a cue; a new bracketed token isn't a valid cue; no cue was added; the output was cut off; "genuine/genuinely" remains |

### Reports (every number computed by code)

```
✅ Script humanized
📄 Doc: https://docs.google.com/document/d/…/edit?tab=t.0

📝 Words: 2,253 → 2,064
🧹 Filler / repeated words removed: genuine(ly) x27, real (filler) x14, actually x10, …
🔁 "It's not X, it's Y" lines rewritten: 4
🔀 Repeated openers varied: "let's talk about" x13 → varied, …
🔎 Checks: intro ✅ outro ✅ Short 1 ✅ Short 2 ✅ names/numbers ✅

👇 Want emotion cues for ElevenLabs? Tap Add Emotion. Skip it if this video doesn't need them.
```

```
🎭 Emotions added
📄 Doc: https://docs.google.com/document/d/…/edit?tab=t.0

🎭 Cues added: 28 (analytical 5, informative 3, emphatic 3, …)
🔎 Checks: script text unchanged ✅ Short headers untouched ✅ no cues on editor notes ✅

🔄 Done. Clear the Doc, paste your next script, then send any message.
```

A "⚠️ Double-check" line appears only when something is flagged. A
per-section cue line appears only when chapter titles are found identically
in the input and output. The "it's not X, it's Y" count comes from regexes
and is approximate.

## Old paste / .txt flow (off)

The original `.txt` upload and paste + `/go` handlers are still in the code
but off by default (`LEGACY_TXT_FLOW=false`). Uploads and `/go` reply with a
pointer to the Doc, and typed text shows the menu.

## Prompts

- [`bot/prompt.txt`](bot/prompt.txt): Humanize pass
- [`bot/emotion_prompt.txt`](bot/emotion_prompt.txt): Emotion pass,
  verbatim. A short format block (cue style, keep paragraph breaks,
  placeholders) is appended in code, so the prompt text itself stays as
  written.

Edit either file to change behaviour; no code change is needed. Filler words,
contrast patterns and protected-line patterns live in
[`bot/rules.py`](bot/rules.py).

## Model choice

Default `gpt-5.5`. Live results on the two sample scripts:

| Model | Real Madrid (2,253 words) | FC 27 "two hundred" tic (15×) | Time per script |
|---|---|---|---|
| gpt-4o | ❌ cut to ~870 words (summarised), stopped by the gate | ❌ 15 → 15 | ~40s |
| gpt-4.1 | ✅ −9.5%, 2 contrast lines left | ❌ 15 → 14 | ~15–20s |
| **gpt-5.5** | ✅ −8.5%, everything cleared | ✅ 15 → 2 | ~50–60s |

## Structure

```
script-humanizer/
├── run.py                  # entry point → bot.main.main()
├── bot/
│   ├── main.py             # Telegram wiring; "go humanize" is matched before the paste handler
│   ├── docmode.py          # menu + 3 buttons, pre-flight, session state, Cancel, one edited message
│   ├── docflow.py          # Go Humanize and Add Emotion steps, their gates and HTML reports
│   ├── emotion.py          # Emotion pass, shared by any entry point; genuine strip; cue helpers
│   ├── emotion_prompt.txt  # Emotion prompt (verbatim)
│   ├── gdoc.py             # Google Docs read / replace (service account)
│   ├── intake.py           # allowlist; old .txt / paste / /go / /cancel flow (off by default)
│   ├── state.py            # persistent hashes + last 3 backups under STATE_DIR
│   ├── pipeline.py         # Humanize pass: protect → edit → restore → scan
│   ├── protect.py          # placeholder swap-out / restore
│   ├── llm.py              # the OpenAI call
│   ├── prompt.txt          # Humanize prompt
│   ├── rules.py            # filler list, contrast regexes, protected patterns
│   ├── scan.py             # scan, fact check, report text
│   ├── config.py           # env vars
│   └── cli.py              # run locally without Telegram
├── tests/                  # unit tests + the two sample scripts in fixtures/
├── requirements.txt
├── requirements-dev.txt
├── railway.json            # NIXPACKS, `python run.py`, restart on failure
├── .env.example
└── .gitignore              # .env and the Google key are never committed
```

## Configuration

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `OPENAI_API_KEY` | OpenAI **API** key (platform.openai.com). A ChatGPT Plus subscription does not include API access. |
| `OPENAI_MODEL` | Default `gpt-5.5` (see [Model choice](#model-choice)) |
| `OPENAI_TEMPERATURE` | Leave empty for gpt-5.x, which rejects it. For example `0.4` for gpt-4.1. |
| `ALLOWED_USER_IDS` | Optional comma-separated Telegram user IDs. **Empty = anyone who finds the bot can use it** (it runs on your OpenAI credit and processes your Doc). When set, anyone else is refused and told their ID. |
| `GOOGLE_DOC_ID` | The ID from the Doc URL (`/d/<ID>/edit`). Empty = Doc mode off. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Railway:** the whole service-account key JSON pasted in |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | **Local:** path to the key file (default `google-service-account.json`, git-ignored) |
| `GOOGLE_DOC_TAB_ID` | Tab to read and write. Default `t.0` (the `tab=` value in the Doc URL). |
| `DOC_WRITE_ENABLED` | `false` (default) = dry run. **`true` = actually replace the Doc** (set this on Railway). |
| `STATE_DIR` | Where hashes and backups live. Default `./storage`. **On Railway, point this at a volume mount** (e.g. `/data`). |
| `LEGACY_TXT_FLOW` | `false` (default). `true` re-enables the old paste / .txt flow. |
| `CUE_STYLE` | `brackets` → `[thoughtful]`, ElevenLabs audio tags for **Eleven v4** (the channel's model), v4 Turbo and v3, `parentheses` → `(thoughtful)`, `none` → no cues (genuine is still stripped) |
| `DOC_MIN_WORDS` | Default `200` |
| `DOC_LENGTH_TOLERANCE` | Default `0.15` (±15%) |
| `CUE_SIMILARITY_MIN` | Default `0.90` |

### Google setup (one time)

1. Create a Google Cloud project and enable the **Google Docs API**.
2. Create a service account (no roles needed) and download a **JSON key**.
3. Share the Doc with the service account's email as **Editor**.

The bot uses only the `documents` scope.

## Setup and running

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env      # then fill it in
python run.py
```

To lock the bot to specific people later, set `ALLOWED_USER_IDS`. With it
set, anyone else who messages the bot is told their user ID.

Without Telegram:

```
python -m bot.cli tests/fixtures/real_madrid_inter.txt --scan-only   # measure tics, no API call
python -m bot.cli my_script.txt                                      # Humanize pass only
python -m pytest                                                     # unit tests
```

## Deploying on Railway

1. Create a new Railway service from the GitHub repo. `railway.json` already
   sets the start command.
2. Add the variables above. Paste the whole contents of the key file into
   `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. **Add a volume** (service → Settings → Volumes → mount path `/data`) and
   set `STATE_DIR=/data`. Without it, the "same script" hashes and the
   backups are wiped on every redeploy or restart. No public domain is
   needed; the bot uses long polling.
4. **Stop the local bot before deploying.** Two copies polling the same bot
   token conflict.

## Known limitations

1. **Cue spacing is guidance.** The model aims for natural spacing but
   doesn't count sentences. The report shows the actual spacing.
2. **The model may edit more than asked.** The placeholders, fact check,
   length gate and edit-only similarity check catch most of this. Subtler
   rewording isn't measured.
3. **The trigger is manual.** Paste a script and forget `go humanize`, and
   nothing happens.
4. **The overwrite is destructive.** That's why there are the saved backups,
   pre-flight checks, the gate, the revision lock and the read-back check.
   Version history is the last fallback.
5. **The Doc becomes plain text** after a run. Headings, bold and bullets are
   not kept.
6. **"Removed" counts come from the filler-word list.** They don't capture
   other rewording.
7. **Two model calls per script.** About double the cost of one, roughly 1
   minute with gpt-5.5.
8. **Deleting "genuine" can't always fix grammar.** Where it was used as a
   predicate ("the passion is genuine."), the report flags the sentence.
9. **Cues are written for Eleven v4.** `[brackets]` audio tags work on
   Eleven v4, v4 Turbo and v3. Older ElevenLabs models (Multilingual v2,
   Flash, Turbo v2.5) read them aloud, so set `CUE_STYLE=none` if you
   switch to one of those.
10. **The sponsor read in a script is kept.** Removing it is a generation
    job, done in the Claude chat prompt.
