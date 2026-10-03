"""Pattern lists used by the protect step and the safety scan.

Everything here is topic-neutral - add new tics or protected-line formats
here without touching the pipeline.
"""

import re

# Filler words the prompt tells the model to cut. Label -> regex.
# "real" is matched lowercase-only so team names like "Real Madrid" don't count.
FILLER_PATTERNS: dict[str, re.Pattern[str]] = {
    "genuine(ly)": re.compile(r"\bgenuine(?:ly)?\b", re.IGNORECASE),
    "honestly": re.compile(r"\bhonestly\b", re.IGNORECASE),
    "actually": re.compile(r"\bactually\b", re.IGNORECASE),
    "really": re.compile(r"\breally\b", re.IGNORECASE),
    "real (filler)": re.compile(r"\breal\b"),
    "exact(ly)": re.compile(r"\bexact(?:ly)?\b", re.IGNORECASE),
    "specific(ally)": re.compile(r"\bspecific(?:ally)?\b", re.IGNORECASE),
    "precisely": re.compile(r"\bprecisely\b", re.IGNORECASE),
    "properly": re.compile(r"\bproperly\b", re.IGNORECASE),
    "literally": re.compile(r"\bliterally\b", re.IGNORECASE),
    "basically": re.compile(r"\bbasically\b", re.IGNORECASE),
    "truly": re.compile(r"\btruly\b", re.IGNORECASE),
    "simply": re.compile(r"\bsimply\b", re.IGNORECASE),
    "<someone's> own": re.compile(r"\b(?:their|his|her|its|my|our|your|[A-Za-z]+'s)\s+own\b", re.IGNORECASE),
}

_SUBJ = r"(?:it|that|this|which)"
_IS = r"(?:'s|\s+is)"
_ISNT = r"(?:'s\s+not|\s+is\s+not|\s+isn't)"

# "It's not X, it's Y" style contrast framing. Text is apostrophe-normalised
# (curly -> straight) before these run.
CONTRAST_PATTERNS: list[re.Pattern[str]] = [
    # It's not X, it's Y / That is not X - it is Y / This isn't X; it's Y
    # (The "it's Y" half is a lookahead so it can start the next match.)
    re.compile(rf"\b{_SUBJ}{_ISNT}\s+[^.!?]{{1,100}}?[,;:—–-](?=\s*(?:it|that|this|they){_IS}\b)", re.IGNORECASE),
    # It's not X. It's Y
    re.compile(rf"\b{_SUBJ}{_ISNT}\s+[^.!?]{{1,100}}[.!?](?=\s+(?:it|that|this){_IS}\b)", re.IGNORECASE),
    # not just X, but Y
    re.compile(r"\bnot\s+(?:just|only|merely|simply)\s+[^.!?]{1,100}?,?\s+but\b", re.IGNORECASE),
    # doesn't just X, he Ys
    re.compile(r"\b(?:doesn't|don't|didn't|isn't|aren't|wasn't)\s+just\s+[^.!?]{1,80}?,\s*(?:he|she|it|they|we|you|i)\b", re.IGNORECASE),
    # This isn't purely/just/only ...
    re.compile(rf"\b{_SUBJ}{_ISNT}\s+(?:purely|just|only|simply|merely)\b", re.IGNORECASE),
    # "That's simply not accurate." / "It's not." (standalone denial)
    re.compile(rf"\b{_SUBJ}{_IS}\s+(?:simply\s+|just\s+)?not\s+(?:true|accurate|the case|right)\b", re.IGNORECASE),
    re.compile(rf"(?:^|(?<=[.!?]\s))(?:it|that|this){_ISNT}[.!]", re.IGNORECASE | re.MULTILINE),
]

# Lines that must reach the output byte-for-byte. The model never sees them.
PROTECTED_LINE_PATTERNS: list[re.Pattern[str]] = [
    # [Editor note: ...] on one line
    re.compile(r"^\s*\[.*\]\s*$"),
    # Short 1 ( Hello Editor ... )  /  Short One. Hello editor, ...  /  Short 1, for the editor: ...
    re.compile(r"^\s*short\s+(?:\d+|one|two|three|four|five)\b.*\beditor\b", re.IGNORECASE),
]

# A line matching one of these protects itself and everything after it.
PROTECTED_TAIL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*difficult(?:\s+|-)to(?:\s+|-)pronounce\b", re.IGNORECASE),
]

STOPWORDS = frozenset(
    """a an the and or but if so of to in on at by for with from into onto as is are was were be been being
    it its it's this that these those there here they them their he him his she her we us our you your i me my
    not no do does did doing have has had having will would can could should might may must just than then
    more most very too also up down out over under again once all any both each few some such only own same
    what which who whom whose when where why how about against between through during before after above below
    off further while because until s t""".split()
)

# Capitalised words that are not names (sentence-internal capitals like "I").
NOT_NAMES = frozenset({"I", "I'm", "I've", "I'd", "I'll", "OK", "Okay"})

NUMBER_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "million": 1_000_000,
    # Ordinals ("first" is left out - it's too common as a non-fact word).
    "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
}
