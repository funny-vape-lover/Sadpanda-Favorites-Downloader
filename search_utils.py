from __future__ import annotations

import re
from typing import Any


NEGATIVE_TAG_SEARCH_PATTERN = re.compile(
    r'''(?<!\S)-tag:(?:"([^"]+)"|'([^']+)'|(\S+))''',
    re.IGNORECASE,
)


def extract_negative_tag_terms(search_text: str) -> tuple[list[str], str]:
    excluded_tags: list[str] = []
    for match in NEGATIVE_TAG_SEARCH_PATTERN.finditer(str(search_text or "")):
        value = next((group for group in match.groups() if group is not None), "")
        cleaned = value.strip().casefold()
        if cleaned:
            excluded_tags.append(cleaned)
    remaining_query = NEGATIVE_TAG_SEARCH_PATTERN.sub(" ", str(search_text or ""))
    return list(dict.fromkeys(excluded_tags)), " ".join(remaining_query.split())


def exclude_items_by_tags(
    items: list[dict[str, Any]],
    excluded_tags: list[str],
) -> list[dict[str, Any]]:
    if not excluded_tags:
        return items
    return [
        item
        for item in items
        if all(
            excluded_tag not in str(item.get("tags", "") or "").casefold()
            for excluded_tag in excluded_tags
        )
    ]
