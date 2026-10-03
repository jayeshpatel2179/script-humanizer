# script-humanizer

A Telegram bot that **edits** a finished video script so it sounds human and
is ready for ElevenLabs. It doesn't generate scripts. You write the script in
Claude chat as usual; this bot cleans it up.

It works on any topic (FC 27 reviews, football previews, anything else). It
looks for language patterns, not topics.

## How it works: Google Doc only

1. Paste the Claude script into the configured Google Doc (tab `t.0`).
   Select all, then paste, so the Doc only ever holds one script.
2. Send **`go humanize`** to the bot in Telegram (`go humanise` and trailing
   `!`/`.` also work; the message must be exactly that).
3. The bot replies **"Processing…"**. That same message is later edited into
   the report, so every run ends as one message. **No file is ever sent.**
4. A copy of the raw script is saved to `STATE_DIR/backups/` (the newest 3
   are kept) before anything changes.
5. **Pass 1, Humanize** (one model call): cuts repeated and filler words,
   varies repeated openers, rewrites "it's not X, it's Y" lines, simplifies
   hard words.
6. **Pass 2, Emotion** (a separate model call): adds Eleven v4 audio tags such
   as `[thoughtful]`. Code then removes any "genuine"/"genuinely" the model
   missed.
7. **Validation gate** (code only). If any check fails, that pass is retried
   once. If it fails again, the message reads "Couldn't finish: <check>. The
   Doc was not changed."
8. The bot replaces the tab's text in a single Google request, reads it back,
   stores hashes of the input and the re-read output, and edits the message
   into the report with the Doc link.

Editor notes (`[Editor note: ...]`), the `Short 1` / `Short 2` header lines
and the `Difficult to pronounce words` list are swapped out for placeholders
before either model call. They come back byte-for-byte and never get cues.
Emotion cues already in a pasted script are kept, in order.

### Pre-flight checks (read-only, no model calls)

In this order:

1. A job is already running → "Already working on a script…"
2. The Doc tab is empty or whitespace → "The Doc is empty…"
3. The text matches the last input or last output (hash after collapsing
   whitespace) → "This is the same script I already processed…"
4. Fewer than `DOC_MIN_WORDS` words → "That's only N words…"

### Safety rules

- **Nothing in the Doc changes until the final text passes the gate.**
- The write is locked to the Doc revision that was read. If someone edits the
  Doc while the bot works, Google rejects the write and the Doc is untouched.
- **Dry run** (`DOC_WRITE_ENABLED=false`, the code default): the result is
  saved under `STATE_DIR/backups/` and the Doc is not touched.
- The backups plus Google Docs version history mean an overwrite can always
  be undone.

### Validation gate

| Check | Fails when |
|---|---|
| Short headers | a `Short 1`/`Short 2` line from the input is missing, changed, out of order, or has no text after it |
| Outro | the input has "if you like(d) watching this" and the output doesn't |
| Chapters | "Chapter <number>" text was added |
| genuine/genuinely | any remain (code strips them, so this is a backstop) |
| Length | the word count, cues excluded, changes more than `DOC_LENGTH_TOLERANCE` (±15%) |
| Cut-off | either model call hit its output limit |
| Existing cues | Pass 1 changed the ordered sequence of cues already in the script, or Pass 2 dropped or reordered any of them |
| Edit-only | Pass 2's text, cues removed, is less than `CUE_SIMILARITY_MIN` (90%) similar to Pass 1's output, meaning it rewrote instead of adding cues |
| Cue placement | a cue landed on a Short header line |

### Report (every number computed by code)

```
Script humanized
Doc: https://docs.google.com/document/d/…/edit?tab=t.0

Words: 2,253 → 2,062
Filler / repeated words removed: genuine(ly) x27, real (filler) x14, actually x10, …
"It's not X, it's Y" lines rewritten: 4
Repeated openers varied: "let's talk about" x13 → varied, "after two hundred hours" x5 → 1
Emotion cues: 0 in, 31 out (analytical 6, informative 5, …)
Checks: intro ok, outro ok, Short 1 ok, Short 2 ok, names/numbers ok
Double-check: <anything flagged>
```

The "it's not X, it's Y" count comes from regexes and is approximate. A
per-section cue line is shown only when chapter titles are found identically
in the input and output.

## Old paste / .txt flow (off)

The original `.txt` upload, paste + `/go` and `/cancel` handlers are still in
the code but off by default. They reply "I work from the Google Doc now…".
Set `LEGACY_TXT_FLOW=true` to turn them back on (Humanize pass only).

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
│   ├── docmode.py          # go humanize: pre-flight, backup, write, one edited report message
│   ├── docflow.py          # Pass 1 → Pass 2 → validation gate → report (no Telegram/Google code)
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
