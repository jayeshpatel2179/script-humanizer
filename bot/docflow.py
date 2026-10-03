"""Doc mode processing: Humanize pass → Emotion pass → validation gate → report.

No Telegram or Google code here, so it can be tested with a fake model.
Every number in the report is computed from the before/after text.
"""

import difflib
import re
from dataclasses import dataclass, field

from bot import config
from bot.emotion import GENUINE_RE, EmotionResult, add_emotion, cue_pattern, strip_cues, strip_genuine
from bot.pipeline import HumanizeResult, humanize
from bot.protect import protect, strip_placeholders
from bot.scan import FILLER_PATTERNS, analyze, compare, word_count

SHORT_HEADER_RES = {
    "Short 1": re.compile(r"^\s*short\s*(?:1|one)\b", re.IGNORECASE),
    "Short 2": re.compile(r"^\s*short\s*(?:2|two)\b", re.IGNORECASE),
}
OUTRO_RE = re.compile(r"if\s+you\s+liked?\s+watching\s+this", re.IGNORECASE)
CHAPTER_RE = re.compile(r"(?im)^\s*chapter\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b")
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")


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


def check_humanize(source: str, result: HumanizeResult) -> list[str]:
    failures, _ = check_structure(source, result.text)
    if result.truncated:
        failures.append("Humanize output was cut off by the model's output limit")
    if failure := _length_failure(source, result.text):
        failures.append(failure)
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
    if not result.skipped:
        expected = strip_genuine(stage1)[0]
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

    return build_outcome(script, stage1.text, stage2)


# --- report ------------------------------------------------------------------


def _body(text: str) -> str:
    """Narration only: protected lines (editor notes, Short headers, pronunciation list) removed."""
    return strip_placeholders(protect(text).text)


def build_outcome(source: str, stage1: str, result: EmotionResult) -> DocOutcome:
    final = result.text.strip() + "\n"
    src_body = _body(source)
    final_body = strip_cues(_body(final))
    before, after = analyze(src_body), analyze(final_body)
    scan = compare(src_body, final_body, length_tolerance=config.DOC_LENGTH_TOLERANCE)

    words_before, words_after = word_count(source), word_count(strip_cues(final))
    change = (words_after - words_before) / words_before if words_before else 0.0
    lines = [f"Words: {words_before:,} → {words_after:,} ({change:+.1%})"]

    genuine_before = len(GENUINE_RE.findall(source))
    caught = f" (code caught {result.genuine_removed_by_code} the model missed)" if result.genuine_removed_by_code else ""
    lines.append(f"genuine/genuinely: {genuine_before} → 0{caught}")

    filler = [f"{label} {before.fillers[label]}→{after.fillers[label]}" for label in FILLER_PATTERNS
              if label != "genuine(ly)" and (before.fillers[label] or after.fillers[label])]
    if filler:
        lines.append("Filler: " + " · ".join(filler[:6]))
    lines.append(f"Contrast framing: {len(before.contrast)} → {len(after.contrast)} · "
                 f"Repeated openers: {sum(before.repeated_openers.values())} → {sum(after.repeated_openers.values())}")

    pattern = cue_pattern()
    if pattern and not result.skipped:
        cues = pattern.findall(final)
        sentences = len(_SENTENCE_RE.findall(final_body))
        spacing = f", about 1 per {sentences / len(cues):.1f} sentences" if cues else ""
        lines.append(f"Emotion cues added: {len(cues)} ({len(set(cues))} kinds{spacing})")
    else:
        lines.append("Emotion cues: off (CUE_STYLE=none)")

    headers = _header_lines(final)
    first_line = next((line for line in final.split("\n") if line.strip()), "")
    marks = {
        "intro": bool(first_line),
        "outro": bool(OUTRO_RE.search(final)),
        "Short 1": "Short 1" in headers,
        "Short 2": "Short 2" in headers,
    }
    lines.append("Structure: " + " · ".join(f"{k} {'✓' if v else '✗'}" for k, v in marks.items()))
    lines.append(f"Fact check: all {scan.facts_total} names/numbers kept" if not scan.facts_missing
                 else f"Fact check: ⚠️ missing {', '.join(scan.facts_missing[:10])}")

    _, structure_warnings = check_structure(source, final)
    warnings = structure_warnings + [f'Check wording where "genuine" was removed: "{s}"' for s in result.grammar_flags[:3]]
    if after.contrast:
        warnings.append(f"Contrast framing left: {after.contrast[0]}")
    return DocOutcome(text=final, report_lines=lines, warnings=warnings)

