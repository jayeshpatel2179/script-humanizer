# script-humanizer

A Telegram bot that **edits** a finished video script so it sounds human and
is ready for ElevenLabs. It doesn't generate scripts. You write the script in
Claude chat as usual; this bot cleans it up.

It works on any topic (FC 27 reviews, football previews, anything else). It
looks for language patterns, not topics.

## Main flow: Google Doc mode

1. Paste the Claude script into the configured Google Doc. Select all, then
   paste, so the Doc only ever holds one script.
2. Send **`go humanize`** to the bot in Telegram (`go humanise` and trailing
   `!`/`.` also work; the message must be exactly that).
3. The bot reads the Doc and sends you the original as a **backup file**
   before anything changes.
4. **Pass 1, Humanize** (one model call): cuts repeated and filler words,
   varies repeated openers, rewrites "it's not X, it's Y" lines, simplifies
   hard words.
5. **Pass 2, Emotion** (a separate model call): adds ElevenLabs delivery cues
   such as `[thoughtful]`. Code then removes any "genuine"/"genuinely" the
   model missed.
6. **Validation gate** (code only). If any check fails, that pass is retried
   once. If it fails again, **the Doc stays untouched** and you're told which
   check failed.
7. The bot replaces the Doc's text with the result in a single Google
   request, reads it back to confirm, and replies with the Doc link and a
   report.

Editor notes (`[Editor note: ...]`), the `Short 1` / `Short 2` header lines
and the `Difficult to pronounce words` list are swapped out for placeholders
before either model call. They come back byte-for-byte and never get cues.

### Safety rules

- **Nothing in the Doc changes until the final text passes the gate.** Any
  failure before the write leaves the Doc as it was.
- The write is locked to the Doc version that was read. If you edit the Doc
  while the bot is working, Google rejects the write and the bot tells you.
- The bot refuses to run when:
  - the Doc is empty or under `DOC_MIN_WORDS`
  - the Doc still holds the bot's last output
  - the Doc already contains emotion cues (already processed, or a new
    script pasted under an old one)
- Only one job runs at a time. A second `go humanize` gets "already
  processing".
- **Dry run by default** (`DOC_WRITE_ENABLED=false`): the result is sent to
  Telegram as a file and the Doc is not touched.
- The backup file plus Google Docs version history mean an overwrite can
  always be undone.

### Validation gate

| Check | Fails when |
|---|---|
| Short headers | a `Short 1`/`Short 2` line from the input is missing, changed, out of order, or has no text after it |
| Outro | the input has "if you like(d) watching this" and the output doesn't |
| Chapters | "Chapter <number>" text was added |
| genuine/genuinely | any remain (code strips them, so this is a backstop) |
| Length | the word count, cues excluded, changes more than `DOC_LENGTH_TOLERANCE` (±15%) |
| Cut-off | either model call hit its output limit |
| Edit-only | Pass 2's text, cues removed, is less than `CUE_SIMILARITY_MIN` (90%) similar to Pass 1's output, meaning it rewrote instead of adding cues |
| Cue placement | a cue landed on a Short header line |

### Report (every number computed by code)

```
✅ Doc updated: https://docs.google.com/document/d/…/edit

Words: 2,253 → 2,062 (-8.5%)
genuine/genuinely: 27 → 0
Filler: actually 10→0 · real (filler) 14→0 · exact(ly) 9→0 · …
Contrast framing: 4 → 0 · Repeated openers: 15 → 2
Emotion cues added: 31 (12 kinds, about 1 per 3.4 sentences)
Structure: intro ✓ · outro ✓ · Short 1 ✓ · Short 2 ✓
Fact check: all 99 names/numbers kept
```

## Other entry point: paste or .txt (Humanize only)

Send a `.txt` file, or paste the script and send `/go`. You get back
`<name>_humanized.txt` and a scan report. This runs Pass 1 only.

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
│   ├── docmode.py          # go humanize: pre-flight, backup, dry run / write, report
│   ├── docflow.py          # Pass 1 → Pass 2 → validation gate → report (no Telegram/Google code)
│   ├── emotion.py          # Emotion pass, shared by any entry point; genuine strip; cue helpers
│   ├── emotion_prompt.txt  # Emotion prompt (verbatim)
│   ├── gdoc.py             # Google Docs read / replace (service account)
│   ├── intake.py           # .txt upload, paste buffering, /go, /cancel, allowlist
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
| `DOC_WRITE_ENABLED` | `false` (default) = dry run. `true` = actually replace the Doc. |
| `CUE_STYLE` | `brackets` → `[thoughtful]` (ElevenLabs v3 audio tags), `parentheses` → `(thoughtful)`, `none` → no cues (genuine is still stripped) |
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
3. No public domain and no volume are needed. The bot uses long polling and
   keeps no state that matters across restarts.
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
4. **The overwrite is destructive.** That's why there's the backup file,
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
9. **Whether the cue format suits your ElevenLabs model isn't confirmed
   yet.** `[brackets]` are v3 audio tags; older models read them aloud.
   Change `CUE_STYLE` if needed.
10. **The sponsor read in a script is kept.** Removing it is a generation
    job, done in the Claude chat prompt.
