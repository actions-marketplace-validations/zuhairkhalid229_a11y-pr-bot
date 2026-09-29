"""Stage 1b: score every line of every changed file against the literals and
cut the top hits into JSX-shaped windows for the model to choose between.

A window is the smallest span that looks like one JSX element around a hit
line: walk up to the nearest opening `<Tag` (≤ MAX_UP lines), walk down to the
matching `/>` or `</Tag>` (≤ MAX_DOWN lines). Rough, but the model sees the
whole element with its props, which is what it needs to write a patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from worker.mapper.literals import ElementLiterals

MAX_CANDIDATES = 3
MAX_UP = 12
MAX_DOWN = 20
_CONTEXT = 2  # extra lines each side of the element for the model's benefit

_OPEN_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9.]*)")
_SELF_CLOSE = re.compile(r"/>")
_CLOSE_TAG = re.compile(r"</([A-Za-z][A-Za-z0-9.]*)>")
# A capitalised tag is a React component call site. Prop-name literals
# (" src=") are evidence only there; on <img src=> they are tautological.
_COMPONENT_TAG = re.compile(r"<[A-Z][A-Za-z0-9.]*")


@dataclass
class Candidate:
    file: str
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive
    excerpt: str  # the lines, without numbering
    score: int
    matched: list[str] = field(default_factory=list)
    has_tag: bool = False


def find_candidates(
    literals: ElementLiterals, files: dict[str, str], *, limit: int = MAX_CANDIDATES
) -> list[Candidate]:
    hits: list[tuple[int, str, int, list[str]]] = []  # (score, file, line_no, matched)

    for path, content in files.items():
        lines = content.splitlines()
        for idx, line in enumerate(lines, start=1):
            score = 0
            matched: list[str] = []
            for lit in literals.literals:
                if lit.kind == "tag":
                    continue  # scored at window level, not line level
                if lit.kind == "prop-name" and not _COMPONENT_TAG.search(line):
                    continue
                if lit.value in line:
                    score += lit.weight
                    matched.append(lit.value)
            if score > 0:
                hits.append((score, path, idx, matched))

    if not hits and literals.tag:
        # No literal survived the projection (all-hashed classes, i18n text,
        # dynamic src). Fall back to every element with that tag; the model
        # gets a fair chance only if there are few.
        hits = _tag_only_hits(literals.tag, files)

    hits.sort(key=lambda h: -h[0])

    windows: list[Candidate] = []
    for score, path, line_no, matched in hits:
        lines = files[path].splitlines()
        start, end = _element_window(lines, line_no)
        if any(w.file == path and not (end < w.line_start or start > w.line_end) for w in windows):
            continue  # overlaps an existing window; merge by skipping
        excerpt_lines = lines[start - 1 : end]
        has_tag = bool(literals.tag) and any(
            re.search(rf"<{re.escape(literals.tag)}\b", ln, re.IGNORECASE) for ln in excerpt_lines
        )
        windows.append(
            Candidate(
                file=path,
                line_start=start,
                line_end=end,
                excerpt="\n".join(excerpt_lines),
                score=score + (2 if has_tag else 0),
                matched=matched,
                has_tag=has_tag,
            )
        )
        if len(windows) >= limit * 2:
            break

    windows.sort(key=lambda w: -w.score)
    return windows[:limit]


def _tag_only_hits(tag: str, files: dict[str, str]) -> list[tuple[int, str, int, list[str]]]:
    pattern = re.compile(rf"<{re.escape(tag)}\b", re.IGNORECASE)
    hits = []
    for path, content in files.items():
        for idx, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                hits.append((1, path, idx, [f"<{tag}"]))
    # More than a handful means the tag alone is not evidence of anything.
    return hits if len(hits) <= MAX_CANDIDATES * 2 else []


def _element_window(lines: list[str], hit: int) -> tuple[int, int]:
    """1-based inclusive (start, end) around the JSX element containing `hit`."""
    n = len(lines)
    start = hit
    for i in range(hit, max(hit - MAX_UP, 1) - 1, -1):
        if _OPEN_TAG.search(lines[i - 1]):
            start = i
            break

    end = hit
    depth = 0
    opened_here = False
    for i in range(start, min(start + MAX_DOWN, n) + 1):
        line = lines[i - 1]
        opens = len(_OPEN_TAG.findall(line))
        selfs = len(_SELF_CLOSE.findall(line))
        closes = len(_CLOSE_TAG.findall(line))
        depth += opens - selfs - closes
        if opens:
            opened_here = True
        end = i
        if opened_here and depth <= 0 and i >= hit:
            break

    start = max(1, start - _CONTEXT)
    end = min(n, end + _CONTEXT)
    return start, end
