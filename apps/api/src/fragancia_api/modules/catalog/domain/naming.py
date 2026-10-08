"""Name rules shared by the catalog aggregates (brands, olfactory families).

A name is trimmed with inner whitespace collapsed, has 2-80 characters and a non-empty slug
(lowercase ASCII words joined by hyphens), which is what identifies it.
"""

import re
import unicodedata

NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 80
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    return _NOT_ALPHANUMERIC.sub("-", ascii_text).strip("-")


def clean_name(raw: str) -> str | None:
    """The normalized name, or None when it breaks the length or character rules."""
    value = " ".join(raw.split())
    if not NAME_MIN_LENGTH <= len(value) <= NAME_MAX_LENGTH or not slugify(value):
        return None
    return value
