from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Set, Tuple


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

_STOPWORDS: Set[str] = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "i",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "s",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "with",
    "without",
    "you",
    "your",
}


def tokenize(text: str) -> List[str]:
    raw = str(text or "").lower()
    tokens = [t for t in _TOKEN_RE.findall(raw) if len(t) >= 2]
    return [t for t in tokens if t not in _STOPWORDS]


def token_set(text: str) -> Set[str]:
    return set(tokenize(text))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = sa.intersection(sb)
    union = sa.union(sb)
    return float(len(inter)) / float(len(union)) if union else 0.0


def cosine_counts(counts_a: Dict[str, int], counts_b: Dict[str, int]) -> float:
    if not counts_a and not counts_b:
        return 1.0
    if not counts_a or not counts_b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for k, va in counts_a.items():
        na += float(va) * float(va)
        vb = counts_b.get(k)
        if vb:
            dot += float(va) * float(vb)
    for vb in counts_b.values():
        nb += float(vb) * float(vb)
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom <= 0:
        return 0.0
    return float(dot) / float(denom)


def token_counts(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for t in tokenize(text):
        counts[t] = counts.get(t, 0) + 1
    return counts


def similarity(text_a: str, text_b: str) -> float:
    """Deterministic similarity score in [0,1]."""
    # Blend set overlap (good for short titles) and cosine counts (good for longer text).
    sa = token_set(text_a)
    sb = token_set(text_b)
    j = jaccard(sa, sb)
    ca = token_counts(text_a)
    cb = token_counts(text_b)
    c = cosine_counts(ca, cb)
    return 0.55 * j + 0.45 * c


def top_k_similar(
    *,
    query_text: str,
    candidates: List[Tuple[str, str]],
    k: int = 5,
    min_score: float = 0.25,
) -> List[Tuple[str, float]]:
    scored: List[Tuple[str, float]] = []
    for ref, cand_text in candidates:
        s = similarity(query_text, cand_text)
        if s >= float(min_score):
            scored.append((ref, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(0, int(k))]

