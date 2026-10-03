"""Doc mode processing, as two separate steps:

- Humanize: Humanize pass → "genuine" strip → gate → report. Adds no cues.
- Emotion:  Emotion-cue pass (with its "genuine" strip) → gate → report.

No Telegram or Google code here, so it can be tested with a fake model.
Every number in a report is computed from the before/after text. Reports are
Telegram HTML, and every dynamic string is escaped.
"""

import difflib
import html
import re
from collections import Counter
from dataclasses import dataclass

from bot import config
from bot.emotion import GENUINE_RE, EmotionResult, add_emotion, cue_pattern, strip_cues, strip_genuine
from bot.pipeline import HumanizeResult, humanize
from bot.protect import protect, strip_placeholders
from bot.scan import FILLER_PATTERNS, TextStats, analyze, compare, word_count

SHORT_HEADER_RES = {
    "Short 1": re.compile(r"^\s*short\s*(?:1|one)\b", re.IGNORECASE),
    "Short 2": re.compile(r"^\s*short\s*(?:2|two)\b", re.IGNORECASE),
}
OUTRO_RE = re.compile(r"if\s+you\s+liked?\s+watching\s+this", re.IGNORECASE)
CHAPTER_RE = re.compile(r"(?im)^\s*chapter\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b")
BRACKET_TOKEN_RE = re.compile(r"\[[^\[\]\n]{1,60}\]")

OK, BAD = "✅", "❌"


class GateFailure(Exception):
    def __init__(self, stage: str, failures: list[str]):
        self.stage, self.failures = stage, failures
        super().__init__(f"{stage}: {'; '.join(failures)}")


@dataclass
class StepOutcome:
    text: str  # what gets written to the Doc
    report_lines: list[str]  # Telegram HTML, without the title/link header and the closing line


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def normalise_ws(text: str) -> str:
    return " ".join(text.split())


# --- structure checks (shared) ------------------------------------------------


def _header_lines(text: str) -> dict[str, tuple[int, str]]:
    """First line index and exact text of each Short header."""
    found: dict[str, tuple[int, str]] = {}
    for i, line in enumerate(text.split("\n")):
        for name, pattern in SHORT_HEADER_RES.items():
            if name not in found and pattern.match(line):
                found[name] = (i, line)
    return found


def _has_body_after(lines: list[str], index: int) -> bool:
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        return not any(p.match(line) for p in SHORT_HEADER_RES.values())
    return False


def check_structure(source: str, output: str) -> tuple[list[str], list[str]]:
    """(failures, warnings) for structure the edit must not break."""
    failures: list[str] = []
    warnings: list[str] = []
    src_headers, out_headers = _header_lines(source), _header_lines(output)
    out_lines = output.split("\n")
    for name, (_, line) in src_headers.items():
        if name not in out_headers:
            failures.append(f"{name} header is missing")
        elif out_headers[name][1] != line:
            failures.append(f"{name} header line was changed")
        elif not _has_body_after(out_lines, out_headers[name][0]):
            failures.append(f"{name} has no script text after its header")
    if "Short 1" in out_headers and "Short 2" in out_headers and out_headers["Short 1"][0] > out_headers["Short 2"][0]:
        failures.append("Short 1 and Short 2 are out of order")
    if len(src_headers) < 2:
        warnings.append("Input has no Short 1 / Short 2 header lines")

    if OUTRO_RE.search(source):
        if not OUTRO_RE.search(output):
            failures.append('Outro line "if you like watching this" is missing')
    else:
        warnings.append('Input has no "if you like watching this" outro')

    if len(CHAPTER_RE.findall(output)) > len(CHAPTER_RE.findall(source)):
        failures.append('"Chapter <number>" text was added')
    return failures, warnings


def _length_failure(source: str, output: str) -> str | None:
    before, after = word_count(source), word_count(strip_cues(output))
    change = (after - before) / before if before else 0.0
    if abs(change) > config.DOC_LENGTH_TOLERANCE:
        return f"Length changed by {change:+.0%} (limit ±{config.DOC_LENGTH_TOLERANCE:.0%})"
    return None


def cue_sequence(text: str) -> list[str]:
    """Cue tokens in order, e.g. ["[thoughtful]", "[curious]"]."""
    pattern = cue_pattern()
    return pattern.findall(text) if pattern else []


def _first_difference(expected: str, actual: str) -> str:
    """Short description of the first word-level difference, for failure messages."""
    a, b = expected.split(), actual.split()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            before = " ".join(a[i1:i2][:6]) or "(nothing)"
            after = " ".join(b[j1:j2][:6]) or "(nothing)"
            return f'"{before}" became "{after}"'
    return "whitespace only"


# --- step 1: Humanize -------------------------------------------------------------


def check_humanize(source: str, result: HumanizeResult, cleaned: str) -> list[str]:
    failures, _ = check_structure(source, cleaned)
    if result.truncated:
        failures.append("Humanize output was cut off by the model's output limit")
    if failure := _length_failure(source, cleaned):
        failures.append(failure)
    # Humanize adds no cues: cues already in the script must come through as the
    # exact same ordered sequence - none dropped, duplicated, reordered or added.
    if cue_sequence(cleaned) != cue_sequence(source):
        failures.append("Emotion cues in the script were dropped, added or reordered")
    return failures


async def run_humanize(script: str) -> StepOutcome:
    """Humanize pass, retried once if the gate fails."""
    for _ in range(2):
        result = await humanize(script)
        cleaned, _, flags = strip_genuine(result.text)
        failures = check_humanize(script, result, cleaned)
        if not failures:
            final = cleaned.strip() + "\n"
            return StepOutcome(text=final, report_lines=humanize_report(script, final, flags))
    raise GateFailure("Go Humanize", failures)


def _body(text: str) -> str:
    """Narration only: protected lines (editor notes, Short headers, pronunciation list) removed."""
    return strip_placeholders(protect(text).text)


def _count_phrase(text: str, phrase: str) -> int:
    return len(re.findall(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE))


def _repeat_items(before: TextStats, after_body: str, limit: int = 4) -> list[str]:
    """Repeated openers and phrases from the input, with how often they appear now (HTML)."""
    after_openers = analyze(after_body).repeated_openers
    items: list[str] = []
    shown: list[str] = []
    for opener, count in list(before.repeated_openers.items())[:2]:
        now = after_openers.get(opener, 0)
        items.append(f'"{esc(opener)}" x{count} → {"varied" if now <= 1 else now}')
        shown.append(opener)
    for phrase, count in before.repeated_phrases:
        if len(items) >= limit:
            break
        if any(phrase in s or s in phrase for s in shown):
            continue
        items.append(f'"{esc(phrase)}" x{count} → {_count_phrase(after_body, phrase)}')
        shown.append(phrase)
    return items


def _double_check_line(warnings: list[str]) -> list[str]:
    return [f"⚠️ <b>Double-check:</b> {esc(' | '.join(warnings))}"] if warnings else []


def humanize_report(source: str, final: str, grammar_flags: list[str]) -> list[str]:
    src_body, final_body = _body(source), strip_cues(_body(final))
    before, after = analyze(strip_cues(src_body)), analyze(final_body)
    scan = compare(strip_cues(src_body), final_body, length_tolerance=config.DOC_LENGTH_TOLERANCE)

    lines = [f"📝 <b>Words:</b> {word_count(strip_cues(source)):,} → {word_count(strip_cues(final)):,}"]

    removed = sorted(((before.fillers[k] - after.fillers[k], k) for k in FILLER_PATTERNS
                      if before.fillers[k] > after.fillers[k]), reverse=True)
    lines.append("🧹 <b>Filler / repeated words removed:</b> "
                 + (", ".join(f"<b>{esc(label)}</b> x{n}" for n, label in removed[:5]) if removed else "none found"))

    # Approximate: counted with the contrast regexes in rules.py (it's/that's/this is ... not ...,
    # it's ...), so unusual phrasings can be missed or over-counted.
    rewritten = max(len(before.contrast) - len(after.contrast), 0)
    left = f" ({len(after.contrast)} left)" if after.contrast else ""
    lines.append(f"🔁 <b>\"It's not X, it's Y\" lines rewritten:</b> {rewritten}{left}")

    if repeats := _repeat_items(before, final_body):
        lines.append("🔀 <b>Repeated openers varied:</b> " + ", ".join(repeats))

    if existing := cue_sequence(source):
        lines.append(f"🎭 <b>Existing cues kept:</b> {len(existing)}")

    headers = _header_lines(final)
    checks = {
        "intro": any(line.strip() for line in final.split("\n")),
        "outro": bool(OUTRO_RE.search(final)),
        "Short 1": "Short 1" in headers,
        "Short 2": "Short 2" in headers,
        "names/numbers": not scan.facts_missing,
    }
    lines.append("🔎 <b>Checks:</b> " + " ".join(f"{k} {OK if v else BAD}" for k, v in checks.items()))

    _, warnings = check_structure(source, final)
    if scan.facts_missing:
        warnings.append(f"Names/numbers missing: {', '.join(scan.facts_missing[:10])}")
    warnings += [f'Wording where "genuine" was removed: "{s}"' for s in grammar_flags[:3]]
    if after.contrast:
        warnings.append(f"\"It's not X, it's Y\" line left: {after.contrast[0]}")
    return lines + _double_check_line(warnings)


# --- step 2: Add Emotion ---------------------------------------------------------


def check_emotion(source: str, result: EmotionResult) -> list[str]:
    final = result.text
    failures: list[str] = []
    if result.truncated:
        failures.append("Emotion output was cut off by the model's output limit")
    if len(cue_sequence(final)) - len(cue_sequence(source)) < 1:
        failures.append("No emotion cues were added")
    if GENUINE_RE.search(final):
        failures.append('"genuine/genuinely" still present')

    # a. Cues are the only change: with every cue removed, the text must equal the input
    #    exactly after whitespace normalisation (the input's own "genuine" words excepted).
    expected = normalise_ws(strip_cues(strip_genuine(source)[0]))
    actual = normalise_ws(strip_cues(final))
    if actual != expected:
        failures.append(f"Script text changed, not just cues added: {_first_difference(expected, actual)}")

    # b. Short headers, editor notes and the pronunciation list are identical and cue-free.
    #    (A cue on one of those lines stops it matching its protected pattern, so it fails here too.)
    def blocks(text: str) -> list[str]:  # trailing whitespace ignored (the script's final newline)
        return [block.rstrip() for block in protect(text).blocks.values()]

    if blocks(final) != blocks(source):
        failures.append("Short headers, editor notes or the pronunciation list changed or got a cue")

    # c. Every new bracketed token is a cue in the configured format.
    pattern = cue_pattern()
    known = set(BRACKET_TOKEN_RE.findall(source))
    odd = [t for t in BRACKET_TOKEN_RE.findall(final) if t not in known and not (pattern and pattern.fullmatch(t))]
    if odd:
        failures.append(f"Cue not in the [emotion] format: {', '.join(odd[:3])}")
    return failures


async def run_emotion(script: str) -> StepOutcome:
    """Emotion-cue pass only, retried once if the gate fails."""
    for _ in range(2):
        result = await add_emotion(script)
        failures = check_emotion(script, result)
        if not failures:
            final = result.text.strip() + "\n"
            return StepOutcome(text=final, report_lines=emotion_report(script, final, result.grammar_flags))
    raise GateFailure("Add Emotion", failures)


def _chapter_titles(body: str) -> list[str]:
    """Best-effort: short standalone lines with no sentence punctuation."""
    titles = []
    for line in body.split("\n"):
        line = strip_cues(line).strip()
        if 1 <= len(line.split()) <= 8 and not re.search(r"[.!?,:;]$", line):
            titles.append(line)
    return titles


def _by_section(source: str, final: str) -> str | None:
    """Cues per section (HTML), only when the same chapter titles are found in input and output."""
    pattern = cue_pattern()
    src_titles, out_titles = _chapter_titles(_body(source)), _chapter_titles(_body(final))
    if pattern is None or len(src_titles) < 2 or [t.lower() for t in src_titles] != [t.lower() for t in out_titles]:
        return None  # detection unreliable - leave the line out
    titles = {t.lower() for t in out_titles}
    sections: list[list] = [["Intro", 0]]
    for line in final.split("\n"):
        bare = strip_cues(line).strip()
        short = next((name for name, p in SHORT_HEADER_RES.items() if p.match(line)), None)
        if bare.lower() in titles:
            sections.append([bare, 0])
        elif short:
            sections.append([short, 0])
        sections[-1][1] += len(pattern.findall(line))
    return "🗂 <b>By section:</b> " + "; ".join(f"{esc(name[:28])}: {count}" for name, count in sections)


def emotion_report(source: str, final: str, grammar_flags: list[str]) -> list[str]:
    # Add Emotion refuses a Doc that already has cues, so every cue in the output is new.
    cues = cue_sequence(final)
    kinds = Counter(c[1:-1] for c in cues).most_common()
    shown = ", ".join(f"{esc(k)} {n}" for k, n in kinds[:8]) + (", ..." if len(kinds) > 8 else "")
    lines = [f"🎭 <b>Cues added:</b> {len(cues) - len(cue_sequence(source))} ({shown})"]
    if section := _by_section(source, final):
        lines.append(section)
    lines.append(f"🔎 <b>Checks:</b> script text unchanged {OK} Short headers untouched {OK} "
                 f"no cues on editor notes {OK}")
    warnings = [f'Wording where "genuine" was removed: "{s}"' for s in grammar_flags[:3]]
    return lines + _double_check_line(warnings)
