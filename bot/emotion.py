"""Emotion-cue pass, shared by every entry point (Doc mode today, a Telegram
button later). One model call adds ElevenLabs delivery cues; code then
guarantees no "genuine"/"genuinely" survives.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from bot import config
from bot.llm import edit_script
from bot.protect import protect, restore

EMOTION_PROMPT = Path(__file__).with_name("emotion_prompt.txt").read_text(encoding="utf-8")

# Sent after the verbatim prompt as a separate block, so the prompt text itself stays untouched.
_FORMAT_BLOCK = """Format rules for this tool:
- Write each cue as {example}: one or two lowercase words, inline, right before the text it applies to, with no colon or dash after it.
- Keep the existing paragraph breaks and line breaks exactly where they are.
- This is an edit pass: return the same script text, with cues added and the prohibited words removed.
- Lines like @@KEEP_1@@ are placeholders for editor notes and headers. Copy them exactly, on their own line, in place, and never put a cue on them.
- Output only the script."""

CUE_STYLES = {
    "brackets": ("[", "]"),
    "parentheses": ("(", ")"),
}

GENUINE_RE = re.compile(r"\bgenuine(?:ly)?\b", re.IGNORECASE)


@dataclass
class EmotionResult:
    text: str
    truncated: bool = False
    genuine_removed_by_code: int = 0
    grammar_flags: list[str] = field(default_factory=list)  # sentences where removal may read oddly
    skipped: bool = False  # CUE_STYLE=none


def cue_pattern(style: str | None = None) -> re.Pattern[str] | None:
    """Regex for one cue in the configured style; None when cues are off.

    Up to three lowercase words, optionally comma-separated - Eleven v4 accepts
    combined tags like [whispering, fearful].
    """
    style = style or config.CUE_STYLE
    if style not in CUE_STYLES:
        return None
    open_, close = (re.escape(c) for c in CUE_STYLES[style])
    return re.compile(rf"{open_}[a-z][a-z-]*(?:,? [a-z][a-z-]*){{0,2}}{close}")


def strip_cues(text: str, style: str | None = None) -> str:
    pattern = cue_pattern(style)
    if pattern is None:
        return text
    stripped = pattern.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"(?m)^[ \t]+", "", stripped))


def system_prompt(style: str | None = None) -> str:
    open_, close = CUE_STYLES[style or config.CUE_STYLE]
    return f"{EMOTION_PROMPT.rstrip()}\n\n{_FORMAT_BLOCK.format(example=f'{open_}thoughtful{close}')}"


async def add_emotion(script: str) -> EmotionResult:
    if config.CUE_STYLE not in CUE_STYLES:
        text, removed, flags = strip_genuine(script)
        return EmotionResult(text=text, genuine_removed_by_code=removed, grammar_flags=flags, skipped=True)

    protected = protect(script)
    edited = await edit_script(protected.text, system_prompt=system_prompt())
    text, removed, flags = strip_genuine(_tidy_cues(edited.text, protected.text))
    restored = restore(text, protected)
    return EmotionResult(text=restored.text.strip() + "\n", truncated=edited.truncated,
                         genuine_removed_by_code=removed, grammar_flags=flags)


def _tidy_cues(text: str, source: str) -> str:
    """Normalise cues the model wrote slightly off-format.

    "[Thoughtful]:" -> "[thoughtful]". Bracketed tokens already in the
    source - e.g. "[VAR]" - are left alone.
    """
    open_, close = (re.escape(c) for c in CUE_STYLES[config.CUE_STYLE])
    cue_re = re.compile(rf"({open_}[A-Za-z][A-Za-z-]*(?:,? [A-Za-z][A-Za-z-]*){{0,2}}{close})(?:[ \t]*[:\u2014\u2013-](?=\s))?")

    def fix(m: re.Match[str]) -> str:
        return m.group(0) if m.group(1) in source else m.group(1).lower()

    return cue_re.sub(fix, text)


def strip_genuine(text: str) -> tuple[str, int, list[str]]:
    """Delete every genuine/genuinely, repairing articles, spacing and capitals.

    Returns (text, removed count, sentences flagged for a human look) - a
    predicate use like "the passion is genuine." can't be fixed by deletion.
    """
    count = len(GENUINE_RE.findall(text))
    if not count:
        return text, 0, []

    # Mid-sentence use followed by punctuation ("the passion is genuine.") - deletion leaves a gap.
    flags: list[str] = []
    for m in re.finditer(r"\w[ \t]+genuine(?:ly)?[ \t]*[.!?,;]", text, re.IGNORECASE):
        start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        flags.append(" ".join(text[start : m.end()].split())[:100])

    # "a genuinely awful" -> "an awful", "a genuine idea" -> "an idea".
    def fix_article(m: re.Match[str]) -> str:
        article, nxt = m.group(1), m.group(2)
        new = "an" if nxt.lower() in "aeiou" else "a"
        return f"{new.capitalize() if article[0].isupper() else new} {nxt}"

    text = re.sub(r"\b([Aa]n?) genuine(?:ly)?,? (\w)", fix_article, text)
    # Sentence-opening "Genuinely, this ..." -> "This ..."
    text = re.sub(r"(^|[.!?]\s+|\n)Genuine(?:ly)?,?\s+(\w)", lambda m: m.group(1) + m.group(2).upper(), text)
    text = GENUINE_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    text = re.sub(r"(?m)^ +", "", text)
    return text, count, flags
