"""Stage 3: never trust the model about the file.

The model claims `original` sits at lines start..end. We check that against
the real bytes. If it is off by a few lines (common: the model counts from the
excerpt and slips), we relocate. If it is nowhere in the file, the claim is
false and the best this finding can get is an annotation at the candidate.

The structural check is deliberately crude: it catches a replacement that
drops a closing brace or leaves a tag open -- the failures that turn a
one-click commit into a broken build -- without needing a JSX parser in the
worker image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_OPEN_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9.]*)(?![^>]*/>)[^>]*>")
_SELF_CLOSE = re.compile(r"<[A-Za-z][^>]*/>")
_CLOSE_TAG = re.compile(r"</[A-Za-z][A-Za-z0-9.]*>")


@dataclass(frozen=True)
class Located:
    line_start: int
    line_end: int
    relocated: bool


def locate_original(
    content: str, original: str, claimed_start: int | None, claimed_end: int | None
) -> Located | None:
    """Find `original` in `content`. Prefer the claimed lines; else search."""
    if not original.strip():
        return None
    lines = content.splitlines()
    orig_lines = original.splitlines()
    n = len(orig_lines)
    if n == 0 or n > len(lines):
        return None

    def matches_at(start: int) -> bool:  # start is 1-based
        window = lines[start - 1 : start - 1 + n]
        return [ln.rstrip() for ln in window] == [ln.rstrip() for ln in orig_lines]

    if claimed_start and 1 <= claimed_start <= len(lines) - n + 1 and matches_at(claimed_start):
        return Located(claimed_start, claimed_start + n - 1, relocated=False)

    # Search nearby first (off-by-N), then the whole file. Require uniqueness
    # for a whole-file match: two identical blocks means we cannot know which.
    if claimed_start:
        for delta in range(1, 6):
            for start in (claimed_start - delta, claimed_start + delta):
                if 1 <= start <= len(lines) - n + 1 and matches_at(start):
                    return Located(start, start + n - 1, relocated=True)

    found = [s for s in range(1, len(lines) - n + 2) if matches_at(s)]
    if len(found) == 1:
        return Located(found[0], found[0] + n - 1, relocated=True)
    return None


def structurally_sound(original: str, replacement: str) -> bool:
    """Balance deltas of the replacement must equal those of the original."""

    def deltas(s: str) -> tuple[int, int, int, int]:
        tags = len(_OPEN_TAG.findall(s)) - len(_CLOSE_TAG.findall(s))
        return (
            s.count("{") - s.count("}"),
            s.count("(") - s.count(")"),
            s.count("[") - s.count("]"),
            tags,
        )

    if not replacement.strip():
        return False
    return deltas(original) == deltas(replacement)


_PLACEHOLDER = re.compile(r"\b(TODO|FIXME|XXX|describe|placeholder|lorem)\b", re.IGNORECASE)


def looks_like_placeholder(replacement: str) -> bool:
    return bool(_PLACEHOLDER.search(replacement))
