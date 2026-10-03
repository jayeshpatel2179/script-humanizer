"""Swap protected lines out for placeholders before the edit, and back after.

The model only ever sees @@KEEP_n@@ where an editor note, a short header or
the pronunciation list was, so it cannot change them.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from bot.rules import PROTECTED_LINE_PATTERNS, PROTECTED_TAIL_PATTERNS

PLACEHOLDER = "@@KEEP_{n}@@"
PLACEHOLDER_RE = re.compile(r"@@\s*KEEP_(\d+)\s*@@")


@dataclass
class Protected:
    text: str  # script with placeholders, sent to the model
    blocks: dict[int, str]  # placeholder number -> original text


@dataclass
class Restored:
    text: str
    reinserted: list[int] = field(default_factory=list)  # model dropped these; put back by position guess
    duplicated: list[int] = field(default_factory=list)  # model repeated these placeholders


def protect(text: str) -> Protected:
    lines = text.split("\n")
    out: list[str] = []
    blocks: dict[int, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        end: int | None = None
        if any(p.match(line) for p in PROTECTED_TAIL_PATTERNS):
            end = len(lines)
        elif any(p.match(line) for p in PROTECTED_LINE_PATTERNS):
            end = i + 1
        elif line.lstrip().startswith("[") and "]" not in line:
            end = _bracket_end(lines, i)  # multi-line [Editor note ...] block
        if end is None:
            out.append(line)
            i += 1
            continue
        n = len(blocks) + 1
        blocks[n] = "\n".join(lines[i:end])
        out.append(PLACEHOLDER.format(n=n))
        i = end
    return Protected(text="\n".join(out), blocks=blocks)


def _bracket_end(lines: list[str], start: int, max_lines: int = 15) -> int | None:
    """Index just past the line that closes a bracket opened on `start`, or None if it never closes nearby."""
    for j in range(start + 1, min(start + 1 + max_lines, len(lines))):
        if "]" in lines[j]:
            return j + 1
    return None


def strip_placeholders(text: str) -> str:
    """Script body without placeholder lines - what the scan measures."""
    return "\n".join(line for line in text.split("\n") if not PLACEHOLDER_RE.fullmatch(line.strip()))


def restore(edited: str, protected: Protected) -> Restored:
    found = Counter(int(m) for m in PLACEHOLDER_RE.findall(edited))
    duplicated = sorted(n for n, count in found.items() if count > 1 and n in protected.blocks)
    reinserted: list[int] = []

    for n in sorted(protected.blocks):
        if n in found:
            continue
        reinserted.append(n)
        token = PLACEHOLDER.format(n=n)
        prev_match = _find(edited, n - 1)
        next_match = _find(edited, n + 1)
        if prev_match:
            edited = f"{edited[: prev_match.end()]}\n\n{token}{edited[prev_match.end():]}"
        elif next_match:
            edited = f"{edited[: next_match.start()]}{token}\n\n{edited[next_match.start():]}"
        else:
            edited = f"{edited.rstrip()}\n\n{token}"

    text = PLACEHOLDER_RE.sub(lambda m: protected.blocks.get(int(m.group(1)), m.group(0)), edited)
    return Restored(text=text, reinserted=reinserted, duplicated=duplicated)


def _find(text: str, n: int) -> re.Match[str] | None:
    if n < 1:
        return None
    return re.search(rf"@@\s*KEEP_{n}\s*@@", text)
