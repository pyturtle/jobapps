"""Mechanical checks that model output is grounded in source text.

A model may only claim something about the posting or about you if it quotes the text that says
so. These functions decide, without any model involved, whether a quote really occurs in the
source (allowing for whitespace, punctuation and tiny wording slips).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

MIN_QUOTE = 12  # characters; shorter "quotes" prove nothing
_TOKEN = re.compile(r"[\w%+#']+")


def _norm(text: str) -> str:
    text = (text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^\w%+#./' -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in(quote: str, source: str, threshold: float = 0.9) -> bool:
    q, s = _norm(quote), _norm(source)
    if len(q) < MIN_QUOTE:
        return False
    if q in s:
        return True
    # Tolerate small slips (a changed word, punctuation): either the longest common run covers
    # nearly all of the quote, or half of it plus 80% of the quote's words near that spot, with
    # every number in the quote present exactly (so an invented metric never passes).
    m = SequenceMatcher(None, s, q, autojunk=False).find_longest_match(0, len(s), 0, len(q))
    if m.size >= threshold * len(q):
        return True
    words = _TOKEN.findall(q)
    if len(words) < 5 or m.size < 0.5 * len(q):
        return False
    nearby = set(_TOKEN.findall(s[max(0, m.a - m.b - 30): m.a - m.b + len(q) + 30]))
    if not all(w in nearby for w in words if any(ch.isdigit() for ch in w)):
        return False
    return sum(w in nearby for w in words) >= 0.8 * len(words)


def phrase_in(quote: str, source: str) -> bool:
    """A quote counts if it's long enough to verify (quote_in), or short ("May 2028") but present word for word."""
    q = _norm(quote)
    if len(q) >= MIN_QUOTE:
        return quote_in(quote, source)
    return bool(q) and re.search(rf"(?<!\w){re.escape(q)}(?!\w)", _norm(source)) is not None


_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|x\b|k\b|\+)?", re.I)


def unsupported_numbers(answer: str, source: str) -> list[str]:
    """Numbers in an answer (40x, 400, 20%) that don't appear anywhere in the source."""
    src = _norm(source).replace(",", "")
    out = []
    for m in _NUMBER.finditer(answer or ""):
        num = m.group(1).replace(",", "")
        if len(num) == 4 and num.startswith("20"):
            continue  # years like 2027 are context, not claims
        if not re.search(rf"(?<![\d.]){re.escape(num)}(?![\d])", src):
            out.append(m.group(0).strip())
    return list(dict.fromkeys(out))


def placeholders(answer: str) -> list[str]:
    return re.findall(r"\[[^\]]{2,120}\]", answer or "")
