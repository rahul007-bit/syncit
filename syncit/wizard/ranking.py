"""Result ranking helpers for wizard search pickers.

Orders results so exact matches come first, then prefix/substring matches,
then fuzzy (difflib ratio) matches — so broad searches surface the package
you meant at the top of the list.
"""

from __future__ import annotations

import difflib
from typing import Any


def _match_score(term: str, name: str) -> float:
    t = term.lower()
    n = name.lower()
    if n == t:
        return 0.0
    if n.startswith(t):
        return 1.0
    if t in n:
        return 2.0
    # fuzzy fallback: 3 - ratio keeps higher-similarity names earlier
    return 3.0 - difflib.SequenceMatcher(None, t, n).ratio()


def rank_matches(term: str, items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Rank (name, summary) results against the search term (best first)."""
    term = term.strip()
    if not term:
        return items
    return sorted(items, key=lambda item: (_match_score(term, item[0]), len(item[0])))
