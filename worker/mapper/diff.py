"""Parse the per-file `patch` strings GitHub returns from
GET /repos/{o}/{r}/pulls/{n}/files into the set of RIGHT-side line numbers
that are inside a hunk.

GitHub's review-comment API only accepts `line` values that appear in the
diff (added or context lines), and a ```suggestion must replace lines that are
all in the diff -- otherwise 422. So "is this line in a hunk" is a hard gate
on posting a suggestion, not a preference.
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def right_side_lines(patch: str | None) -> set[int]:
    """Line numbers on the new-file side that are added or context lines."""
    if not patch:
        return set()
    lines: set[int] = set()
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        m = _HUNK.match(raw)
        if m:
            new_line = int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:  # context line (starts with space, or empty)
            lines.add(new_line)
            new_line += 1
    return lines


def span_in_diff(diff_lines: set[int], start: int, end: int) -> bool:
    return all(n in diff_lines for n in range(start, end + 1))
