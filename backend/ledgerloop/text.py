"""Small, deterministic text helpers for matching company names."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "sa", "plc", "bv", "ag", "pty", "the",
}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """'The ACME Corp., Inc.' -> 'acme'"""
    s = name.lower().replace("&", " and ")
    s = _NON_ALNUM.sub(" ", s)
    tokens = [t for t in _SPACES.split(s) if t and t not in _SUFFIXES]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalized names."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def block_key(norm: str) -> str:
    """Cheap blocking key so we only fuzzy-compare names that share a prefix."""
    return norm[:3] if norm else ""
