"""Small text helpers shared by ingestion, the offline drafter and the validators."""

from __future__ import annotations

import hashlib
import re

# Paragraph references in the (fictional) Northbridge Accounting Standards Board codification,
# e.g. "NASB 116.22". Production matched the provider's real standards references.
REF_RE = re.compile(r"\bNASB \d{3}\.\d{1,3}\b")

STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were "
    "will with which when what who how can should would after before under over into than then "
    "their they them these those not no do does did if each per any all".split()
)


def paragraph_refs(text: str) -> list[str]:
    seen: list[str] = []
    for m in REF_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def tokens(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if t in STOPWORDS or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def trim_words(text: str, limit: int) -> str:
    """Cut to ``limit`` words, at the last clause break in the second half if there is one."""
    words = text.split()
    if len(words) <= limit:
        return text
    cut = words[:limit]
    for i in range(len(cut) - 1, limit // 2, -1):
        if cut[i].endswith((",", ";")):
            cut = cut[: i + 1]
            break
    return " ".join(cut).rstrip(",;:") + "..."


def shingles(text: str, n: int = 8) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def stable_hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
