"""Doc mode processing: Humanize pass → Emotion pass → validation gate → report.

No Telegram or Google code here, so it can be tested with a fake model.
Every number in the report is computed from the before/after text.
"""

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

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


class GateFailure(Exception):
    def __init__(self, stage: str, failures: list[str]):
        self.stage, self.failures = stage, failures
        super().__init__(f"{stage}: {'; '.join(failures)}")


@dataclass
class DocOutcome:
    text: str
    report_lines: list[str]
    warnings: list[str] = field(default_factory=list)


# --- validation gate ---------------------------------------------------------


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


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(token in it for token in needle)


def check_humanize(source: str, result: HumanizeResult) -> list[str]:
    failures, _ = check_structure(source, result.text)
    if result.truncated:
        failures.append("Humanize output was cut off by the model's output limit")
    if failure := _length_failure(source, result.text):
        failures.append(failure)
    # The Humanize pass adds no cues, so cues already in the script must come through
    # as the exact same ordered sequence - none dropped, duplicated or reordered.
    if cue_sequence(result.text) != cue_sequence(source):
        failures.append("Emotion cues already in the script were dropped, duplicated or reordered")
    return failures


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().split(), b.lower().split(), autojunk=False).ratio()


def check_emotion(source: str, stage1: str, result: EmotionResult) -> list[str]:
    final = result.text
    failures, _ = check_structure(source, final)
    if result.truncated:
        failures.append("Emotion output was cut off by the model's output limit")
    if GENUINE_RE.search(final):
        failures.append('"genuine/genuinely" still present')
    if failure := _length_failure(source, final):
        failures.append(failure)
    # This pass adds cues on purpose, so the input's cues must all still be there,
    # in order, with new ones allowed in between.
    if not _is_subsequence(cue_sequence(source), cue_sequence(final)):
        failures.append("Emotion cues already in the script were dropped or reordered")
    if not result.skipped:
        # Cues come off both sides, so cues already in the script don't count as rewriting.
        expected = strip_cues(strip_genuine(stage1)[0])
        score = similarity(expected, strip_cues(final))
        if score < config.CUE_SIMILARITY_MIN:
            failures.append(f"Emotion pass rewrote text instead of only adding cues "
                            f"(similarity {score:.0%}, need {config.CUE_SIMILARITY_MIN:.0%})")
        pattern = cue_pattern()
        for name, (_, line) in _header_lines(final).items():
            if pattern and pattern.search(line):
                failures.append(f"Cue placed on the {name} header line")
    return failures


# --- the two stages, each retried once on a failed gate ------------------------


async def process_script(script: str) -> DocOutcome:
    stage1 = await humanize(script)
    if failures := check_humanize(script, stage1):
        stage1 = await humanize(script)
        if failures := check_humanize(script, stage1):
            raise GateFailure("Humanize pass", failures)

    stage2 = await add_emotion(stage1.text)
    if failures := check_emotion(script, stage1.text, stage2):
        stage2 = await add_emotion(stage1.text)
        if failures := check_emotion(script, stage1.text, stage2):
            raise GateFailure("Emotion pass", failures)

    return build_outcome(script, stage2)


# --- report: every number computed here, from before/after text --------------


def _body(text: str) -> str:
    """Narration only: protected lines (editor notes, Short headers, pronunciation list) removed."""
    return strip_placeholders(protect(text).text)


def _count_phrase(text: str, phrase: str) -> int:
    return len(re.findall(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE))


def _repeat_items(before: TextStats, after_body: str, limit: int = 4) -> list[str]:
    """Repeated openers and phrases from the input, with how often they appear now."""
    after_openers = analyze(after_body).repeated_openers
    items: list[str] = []
    shown: list[str] = []
    for opener, count in list(before.repeated_openers.items())[:2]:
        now = after_openers.get(opener, 0)
        items.append(f'"{opener}" x{count} → {"varied" if now <= 1 else now}')
        shown.append(opener)
    for phrase, count in before.repeated_phrases:
        if len(items) >= limit:
            break
        if any(phrase in s or s in phrase for s in shown):
            continue
        items.append(f'"{phrase}" x{count} → {_count_phrase(after_body, phrase)}')
        shown.append(phrase)
    return items


def _chapter_titles(body: str) -> list[str]:
    """Best-effort: short standalone lines with no sentence punctuation."""
    titles = []
    for line in body.split("\n"):
        line = strip_cues(line).strip()
        if 1 <= len(line.split()) <= 8 and not re.search(r"[.!?,:;]$", line):
            titles.append(line)
    return titles


def _by_section(source: str, final: str) -> str | None:
    """Cue count per section, only when the same chapter titles are found in input and output."""
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
    return "By section (cues): " + " · ".join(f"{name[:28]} {count}" for name, count in sections)


def build_outcome(source: str, result: EmotionResult) -> DocOutcome:
    final = result.text.strip() + "\n"
    src_body = _body(source)
    final_body = strip_cues(_body(final))
    before, after = analyze(src_body), analyze(final_body)
    scan = compare(src_body, final_body, length_tolerance=config.DOC_LENGTH_TOLERANCE)

    lines = [f"Words: {word_count(strip_cues(source)):,} → {word_count(strip_cues(final)):,}"]

    removed = sorted(((before.fillers[k] - after.fillers[k], k) for k in FILLER_PATTERNS
                      if before.fillers[k] > after.fillers[k]), reverse=True)
    lines.append("Filler / repeated words removed: "
                 + (", ".join(f"{label} x{n}" for n, label in removed[:5]) if removed else "none found"))

    # Approximate: counted with the contrast regexes in rules.py (it's/that's/this is ... not ...,
    # it's ...), so unusual phrasings can be missed or over-counted.
    rewritten = max(len(before.contrast) - len(after.contrast), 0)
    left = f" ({len(after.contrast)} left)" if after.contrast else ""
    lines.append(f"\"It's not X, it's Y\" lines rewritten: {rewritten}{left}")

    if repeats := _repeat_items(before, final_body):
        lines.append("Repeated openers varied: " + ", ".join(repeats))

    cues_in, cues_out = cue_sequence(source), cue_sequence(final)
    if result.skipped:
        lines.append(f"Emotion cues: {len(cues_in)} in, {len(cues_out)} out (adding cues is off)")
    else:
        kinds = Counter(c[1:-1] for c in cues_out).most_common(6)
        detail = f" ({', '.join(f'{k} {n}' for k, n in kinds)})" if kinds else ""
        lines.append(f"Emotion cues: {len(cues_in)} in, {len(cues_out)} out{detail}")

    if section_line := _by_section(source, final):
        lines.append(section_line)

    headers = _header_lines(final)
    checks = {
        "intro": any(line.strip() for line in final.split("\n")),
        "outro": bool(OUTRO_RE.search(final)),
        "Short 1": "Short 1" in headers,
        "Short 2": "Short 2" in headers,
        "names/numbers": not scan.facts_missing,
    }
    lines.append("Checks: " + ", ".join(f"{k} {'ok' if v else 'missing'}" for k, v in checks.items()))

    _, warnings = check_structure(source, final)
    if scan.facts_missing:
        warnings.append(f"Names/numbers missing: {', '.join(scan.facts_missing[:10])}")
    warnings += [f'Wording where "genuine" was removed: "{s}"' for s in result.grammar_flags[:3]]
    if after.contrast:
        warnings.append(f"\"It's not X, it's Y\" line left: {after.contrast[0]}")
    return DocOutcome(text=final, report_lines=lines, warnings=warnings)
