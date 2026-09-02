"""Profile-scoped identity for public gateway task-card progress."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from hermes_cli.profiles import read_profile_meta

GENERIC_TASK_CARD_TITLE = "Hermes is working"
_MAX_DISPLAY_NAME_CHARS = 64
_MAX_SOURCE_CHARS = 4096
_ZERO_WIDTH_JOINER = "\u200d"
_UNSAFE_BIDI = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)


def _clean_display_name(value: object) -> str:
    """Return a bounded, single-line display name safe for a public title."""
    if not isinstance(value, str):
        return ""

    source = unicodedata.normalize("NFC", value[:_MAX_SOURCE_CHARS])
    cleaned: list[str] = []
    for char in source:
        if char.isspace():
            cleaned.append(" ")
            continue
        if char in _UNSAFE_BIDI:
            continue
        if unicodedata.category(char).startswith("C") and char != _ZERO_WIDTH_JOINER:
            continue
        cleaned.append(char)

    result = re.sub(r" +", " ", "".join(cleaned)).strip()
    result = result[:_MAX_DISPLAY_NAME_CHARS].rstrip()
    while result and (
        result.endswith(_ZERO_WIDTH_JOINER)
        or unicodedata.category(result[-1]).startswith("M")
    ):
        result = result[:-1].rstrip()
    return result


def resolve_task_card_title(profile_home: str | Path) -> str:
    """Resolve one profile's public working title, with the legacy fallback."""
    try:
        display_name = _clean_display_name(
            read_profile_meta(Path(profile_home)).get("display_name")
        )
    except Exception:
        display_name = ""
    return (
        f"{display_name} is working" if display_name else GENERIC_TASK_CARD_TITLE
    )
