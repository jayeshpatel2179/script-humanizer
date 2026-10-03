"""Code-side safety scan. No AI - just counts, regexes and a before/after diff.

Problems are reported, never silently fixed.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from bot.rules import CONTRAST_PATTERNS, FILLER_PATTERNS, NOT_NAMES, NUMBER_WORDS, STOPWORDS

_WORD_RE = re.compile(r"[^\W_][\w'-]*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_DIGITS_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class TextStats:
    words: int
    fillers: dict[str, int]
    contrast: list[str]  # matching snippets
    repeated_openers: dict[str, int]  # opener -> count, only those used 2+ times
    repeated_phrases: list[tuple[str, int]]  # 3-5 word phrases used 3+ times
    flesch: float


@dataclass
class ScanResult:
    before: TextStats
    after: TextStats
    facts_total: int
    facts_missing: list[str]
    length_change: float  # fraction, e.g. -0.06
    warnings: list[str] = field(default_factory=list)


def normalise(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def analyze(body: str) -> TextStats:
    body = normalise(body)
    return TextStats(
        words=word_count(body),
        fillers={label: len(p.findall(body)) for label, p in FILLER_PATTERNS.items()},
        contrast=_contrast_hits(body),
        repeated_openers=_repeated_openers(body),
        repeated_phrases=_repeated_phrases(body),
        flesch=_flesch(body),
    )


def compare(before_body: str, after_body: str, length_tolerance: float) -> ScanResult:
    before, after = analyze(before_body), analyze(after_body)
    facts = extract_facts(before_body)
    missing = [f for f in sorted(facts) if not _fact_present(f, numbers_to_digits(normalise(after_body)))]
    change = (after.words - before.words) / before.words if before.words else 0.0

    warnings: list[str] = []
    if missing:
        warnings.append(f"Names/numbers missing from the output: {', '.join(missing[:15])}")
    if after.contrast:
        warnings.append(f"Contrast framing still present ({len(after.contrast)})")
    if after.repeated_openers:
        warnings.append("Some paragraphs still open the same way")
    if abs(change) > length_tolerance:
        warnings.append(f"Length changed by {change:+.0%} (limit ±{length_tolerance:.0%})")
    return ScanResult(before, after, len(facts), missing, change, warnings)


# --- individual checks -------------------------------------------------------


def _contrast_hits(body: str) -> list[str]:
    spans: list[tuple[int, int]] = []
    for pattern in CONTRAST_PATTERNS:
        for m in pattern.finditer(body):
            if not any(s <= m.start() < e for s, e in spans):
                spans.append((m.start(), m.end()))
    spans.sort()
    return [_snippet(body, s, e) for s, e in spans]


def _snippet(body: str, start: int, end: int, limit: int = 90) -> str:
    text = " ".join(body[start:end].split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in body.split("\n") if p.strip()]


def _repeated_openers(body: str, words: int = 3) -> dict[str, int]:
    openers = Counter()
    for para in _paragraphs(body):
        tokens = [t.lower() for t in _WORD_RE.findall(para)[:words]]
        if len(tokens) == words:
            openers[" ".join(tokens)] += 1
    return {k: v for k, v in openers.most_common() if v > 1}


def _repeated_phrases(body: str, min_count: int = 3, top: int = 6) -> list[tuple[str, int]]:
    tokens = _WORD_RE.findall(body)
    lower = [t.lower() for t in tokens]
    counts: Counter[str] = Counter()
    for n in (3, 4, 5):
        for i in range(len(tokens) - n + 1):
            gram = lower[i : i + n]
            if all(t in STOPWORDS for t in gram):
                continue
            if all(t[0].isupper() for t in tokens[i : i + n]):
                continue  # a name like "Trent Alexander Arnold"
            counts[" ".join(gram)] += 1
    frequent = {g: c for g, c in counts.items() if c >= min_count}
    # Drop phrases that only repeat because a longer repeated phrase contains them.
    kept = [g for g, c in frequent.items() if not any(g != o and g in o and frequent[o] >= c for o in frequent)]
    kept.sort(key=lambda g: (-frequent[g], -len(g)))
    return [(g, frequent[g]) for g in kept[:top]]


def _syllables(word: str) -> int:
    word = word.lower().strip("'")
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and not word.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(count, 1)


def _flesch(body: str) -> float:
    words = _WORD_RE.findall(body)
    sentences = [s for s in re.split(r"[.!?]+", body) if _WORD_RE.search(s)]
    if not words or not sentences:
        return 0.0
    syllables = sum(_syllables(w) for w in words)
    return 206.835 - 1.015 * (len(words) / len(sentences)) - 84.6 * (syllables / len(words))


# --- fact check -------------------------------------------------------------


_ONES = {w: v for w, v in NUMBER_WORDS.items() if v < 20 and not w.endswith(("st", "nd", "rd", "th"))}
_TENS = {w: v for w, v in NUMBER_WORDS.items() if v in range(20, 100, 10) and w.endswith("ty")}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_CARDINAL = "|".join(sorted([*_ONES, *_TENS, *_SCALES], key=len, reverse=True))
_NUMBER_RUN_RE = re.compile(rf"\b(?:{_CARDINAL})(?:[ -]+(?:{_CARDINAL}))*\b", re.IGNORECASE)


def _parse_number_run(words: list[str]) -> list[int]:
    """["two", "hundred"] -> [200]; ["twenty", "twenty", "six"] -> [20, 26]; ["four", "three"] -> [4, 3]."""
    numbers: list[int] = []
    total = current = 0
    started = False

    def flush():
        nonlocal total, current, started
        if started:
            numbers.append(total + current)
        total = current = 0
        started = False

    for word in words:
        if word in _SCALES:
            scale = _SCALES[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        else:
            value = _ONES.get(word, _TENS.get(word))
            last_two = current % 100
            # A word that can't extend the current number starts a new one ("four three", "twenty twenty").
            if started and (last_two and (value >= 10 or last_two < 20 or last_two % 10)):
                flush()
            current += value
        started = True
    flush()
    return numbers


def numbers_to_digits(text: str) -> str:
    """Spelled-out cardinals to digits, so "two hundred hours" and "200 hours" compare equal."""
    def convert(m: re.Match[str]) -> str:
        words = [w.lower() for w in re.split(r"[ -]+", m.group(0))]
        return " ".join(str(n) for n in _parse_number_run(words))

    return _NUMBER_RUN_RE.sub(convert, text)


def extract_facts(body: str) -> set[str]:
    """Names (capitalised mid-sentence words) and numbers that must survive the edit.

    Spelled-out numbers are read as one value ("two hundred" -> "200"), so a
    rewrite to digits doesn't look like a missing fact.
    """
    body = numbers_to_digits(normalise(body))
    facts: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        for i, token in enumerate(_WORD_RE.findall(sentence)):
            if token.endswith("'s"):
                token = token[:-2]
            for part in token.split("-"):
                if not part:
                    continue
                lowered = part.lower()
                if lowered in NUMBER_WORDS or _DIGITS_RE.fullmatch(part):
                    facts.add(lowered)
                elif i > 0 and part[0].isupper() and part not in NOT_NAMES:
                    facts.add(part)
    return facts


def _fact_present(fact: str, text: str) -> bool:
    if fact in NUMBER_WORDS or _DIGITS_RE.fullmatch(fact):
        value = NUMBER_WORDS.get(fact)
        forms = {re.escape(fact)}
        if value is not None:
            forms.add(str(value))
            forms.update(w for w, v in NUMBER_WORDS.items() if v == value)
        else:
            forms.update(re.escape(w) for w, v in NUMBER_WORDS.items() if str(v) == fact)
        pattern = rf"(?<!\w)(?:{'|'.join(sorted(forms))})(?:st|nd|rd|th)?(?!\w)"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return re.search(rf"(?<!\w){re.escape(fact)}(?!\w)", text) is not None


# --- report -----------------------------------------------------------------


def build_report(result: ScanResult, *, protected_total: int, reinserted: list[int], duplicated: list[int],
                 truncated: bool) -> str:
    b, a = result.before, result.after
    lines = [f"{'⚠️ Done, with warnings' if result.warnings or reinserted or truncated else '✅ Humanized'}",
             f"Words: {b.words:,} → {a.words:,} ({result.length_change:+.1%})", ""]

    filler = [f"{label} {b.fillers[label]}→{a.fillers[label]}" for label in FILLER_PATTERNS
              if b.fillers[label] or a.fillers[label]]
    lines.append("Filler words: " + (" · ".join(filler) if filler else "none"))
    lines.append(f"Contrast framing (\"it's not X, it's Y\"): {len(b.contrast)} → {len(a.contrast)}")
    for snippet in a.contrast[:3]:
        lines.append(f"   • {snippet}")

    if b.repeated_openers or a.repeated_openers:
        before_txt = ", ".join(f'"{k}" ×{v}' for k, v in list(b.repeated_openers.items())[:3]) or "none"
        after_txt = ", ".join(f'"{k}" ×{v}' for k, v in list(a.repeated_openers.items())[:3]) or "none"
        lines.append(f"Repeated openers: {before_txt} → {after_txt}")
    else:
        lines.append("Repeated openers: none")

    if a.repeated_phrases:
        lines.append("Most repeated phrases now: " + ", ".join(f'"{g}" ×{c}' for g, c in a.repeated_phrases[:4]))
    else:
        lines.append("Most repeated phrases now: none used 3+ times")

    lines.append(f"Readability (Flesch, higher = easier): {b.flesch:.0f} → {a.flesch:.0f}")

    kept = protected_total - len(reinserted)
    lines.append(f"Protected lines: {kept}/{protected_total} kept in place" if protected_total
                 else "Protected lines: none found")
    if reinserted:
        lines.append(f"   • The model dropped {len(reinserted)} protected line(s); re-inserted them - check their position")
    if duplicated:
        lines.append(f"   • {len(duplicated)} protected line(s) now appear twice - delete the extra copy")

    if result.facts_missing:
        lines.append(f"Fact check: ⚠️ {len(result.facts_missing)} of {result.facts_total} names/numbers missing: "
                     + ", ".join(result.facts_missing[:15]))
    else:
        lines.append(f"Fact check: all {result.facts_total} names/numbers kept")

    if truncated:
        lines.append("")
        lines.append("⚠️ The model hit its output limit - the end of the script may be cut off.")
    if result.warnings:
        lines.append("")
        lines.append("Check before recording:")
        lines.extend(f"   • {w}" for w in result.warnings)
    return "\n".join(lines)


def build_analysis_report(stats: TextStats) -> str:
    """Single-text report, used by the CLI's --scan-only mode."""
    lines = [f"Words: {stats.words:,}"]
    filler = [f"{label} {count}" for label, count in stats.fillers.items() if count]
    lines.append("Filler words: " + (" · ".join(filler) if filler else "none"))
    lines.append(f"Contrast framing: {len(stats.contrast)}")
    lines.extend(f"   • {s}" for s in stats.contrast[:8])
    lines.append("Repeated openers: " + (", ".join(f'"{k}" ×{v}' for k, v in stats.repeated_openers.items()) or "none"))
    lines.append("Repeated phrases: " + (", ".join(f'"{g}" ×{c}' for g, c in stats.repeated_phrases) or "none"))
    lines.append(f"Readability (Flesch): {stats.flesch:.0f}")
    return "\n".join(lines)
