"""Simple moderation utilities.

This module contains deterministic, testable helpers (no network) so unit-tests
can validate behavior without starting the bot.

Enhancements:
- Use regular expressions with word-boundaries for safer matching.
- Support raw regex entries prefixed with "re:" in the banned list.
"""
from typing import Sequence
import re

# Minimal default banned words. Real projects should use a configurable source.
BANNED_WORDS: Sequence[str] = (
    "badword",
    "spam",
    "scam",
)


def _compile_pattern(banned: Sequence[str]) -> re.Pattern:
    """Compile a single regex Pattern from banned entries.

    Rules:
    - If an entry starts with "re:" the remainder is treated as a raw regex.
    - Otherwise the entry is escaped and matched with word boundaries (\b).
    - Matching is case-insensitive.
    """
    parts: list[str] = []
    for token in banned:
        if token.startswith("re:"):
            parts.append(f"(?:{token[3:]})")
        else:
            parts.append(rf"\b{re.escape(token)}\b")
    pattern = "|".join(parts) if parts else r"$^"  # never matches when empty
    return re.compile(pattern, re.IGNORECASE)


def is_bad_message(text: str | None, banned: Sequence[str] | None = None) -> bool:
    """Return True if `text` contains any banned pattern.

    By default uses `BANNED_WORDS` and matches whole words. To provide custom
    regex patterns prefix them with "re:" in the `banned` sequence, e.g.
    `['re:spam\d+', 'phish']` will match "spam123" and the word "phish".
    """
    if not text:
        return False
    if banned is None:
        banned = BANNED_WORDS
    pattern = _compile_pattern(banned)
    return bool(pattern.search(text))
