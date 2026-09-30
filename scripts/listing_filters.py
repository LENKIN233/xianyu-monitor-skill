"""Shared literal title filters for searches, saved tasks, and result lists."""

from __future__ import annotations

import unicodedata
from typing import Any

MAX_EXCLUSIONS = 20
MAX_EXCLUSION_CHARS = 80


def normalize_exclusions(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EXCLUSIONS:
        raise ValueError("exclude keywords must be a list of at most 20 strings")
    terms: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError(  # noqa: TRY004 - CLI validation uses ValueError.
                "each exclude keyword must be a string"
            )
        term = unicodedata.normalize("NFKC", raw).strip().casefold()
        if not term or len(term) > MAX_EXCLUSION_CHARS or not term.isprintable():
            raise ValueError(
                "exclude keywords must contain 1 to 80 printable characters"
            )
        terms.add(term)
    return sorted(terms)


def exclude_titles(
    items: list[dict[str, Any]], terms: list[str]
) -> list[dict[str, Any]]:
    normalized = normalize_exclusions(terms)
    if not normalized:
        return list(items)
    result = []
    for item in items:
        if not title_is_excluded(item.get("title"), normalized):
            result.append(item)
    return result


def title_is_excluded(title: Any, normalized_terms: list[str]) -> bool:
    if not isinstance(title, str):
        return False
    text = unicodedata.normalize("NFKC", title).casefold()
    return any(term in text for term in normalized_terms)
