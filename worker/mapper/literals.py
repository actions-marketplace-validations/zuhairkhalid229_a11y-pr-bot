"""Stage 1a: pull greppable literals out of a rendered element.

The rendered DOM is a lossy projection of the JSX. Some things survive the
projection intact and are worth grepping for; some are transformed and are
noise. This module encodes that judgement as weights.

    survives        data-testid, id (non-generated), href, src, name, placeholder,
                    aria-label, deterministic class tokens (tailwind, BEM), text
    transformed     hashed classes, useId ids, runtime-added data-* / aria-*
    ambiguous       tag name (an <img> is an <img> in a thousand places)
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from worker.schema import _GENERATED_CLASS, _GENERATED_ID

_TAG = re.compile(r"^\s*<([a-zA-Z][a-zA-Z0-9-]*)")
_ATTR = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')
_TEXT = re.compile(r">([^<]+)<")
_WS = re.compile(r"\s+")

# Attribute -> weight. Higher = rarer in a codebase = stronger evidence.
_ATTR_WEIGHTS = {
    "data-testid": 6,
    "data-test": 6,
    "data-cy": 6,
    "id": 5,
    "href": 4,
    "src": 4,
    "srcset": 3,
    "name": 3,
    "placeholder": 4,
    "aria-label": 4,
    "aria-labelledby": 3,
    "alt": 3,
    "title": 3,
    "for": 3,
    "value": 2,
    "type": 1,
    "role": 1,
}
_CLASS_WEIGHT = 1
_CLASS_MAX_TOTAL = 4
_TEXT_WEIGHT = 4
_TAG_WEIGHT = 1
_SRC_BASENAME_WEIGHT = 3
_SRC_STEM_WEIGHT = 3
# Attributes that React call sites pass straight through as props. When the
# rendered element carries one, `<Avatar src={...}>` is a plausible source
# even though no <img> appears in the changed files.
_PASSTHROUGH_PROPS = {"src": 2, "href": 2, "placeholder": 2, "name": 1, "alt": 2}
_PROP_KIND = "prop-name"
_MIN_LITERAL_LEN = 3
_MAX_TEXT_LEN = 80
# Tokens so common that a hit means nothing.
_STOP_CLASSES = {
    "flex",
    "grid",
    "block",
    "hidden",
    "relative",
    "absolute",
    "container",
    "row",
    "col",
    "btn",
    "button",
    "link",
    "item",
    "active",
    "w-full",
    "h-full",
}


@dataclass(frozen=True)
class Literal:
    value: str
    weight: int
    kind: str  # attr:<name> | class | text | tag | src-basename


@dataclass
class ElementLiterals:
    tag: str | None
    literals: list[Literal] = field(default_factory=list)

    @property
    def strong(self) -> list[Literal]:
        return [lit for lit in self.literals if lit.weight >= 3]


def extract(html: str) -> ElementLiterals:
    tag_match = _TAG.match(html)
    tag = tag_match.group(1).lower() if tag_match else None
    out: list[Literal] = []

    if tag:
        out.append(Literal(f"<{tag}", _TAG_WEIGHT, "tag"))

    # Attributes of the *outer* element only: stop at the first '>'.
    head = html.split(">", 1)[0]
    for name, raw in _ATTR.findall(head):
        value = htmlmod.unescape(raw).strip()
        lname = name.lower()
        if not value:
            continue

        if lname == "class":
            budget = _CLASS_MAX_TOTAL
            for token in value.split():
                if budget <= 0:
                    break
                if len(token) < _MIN_LITERAL_LEN or token in _STOP_CLASSES:
                    continue
                if _GENERATED_CLASS.fullmatch(token):
                    continue
                out.append(Literal(token, _CLASS_WEIGHT, "class"))
                budget -= 1
            continue

        if lname == "id" and _GENERATED_ID.search(f'id="{value}"'):
            continue
        if lname.startswith("data-react") or lname == "style":
            continue

        weight = _ATTR_WEIGHTS.get(lname)
        if weight is None:
            continue
        if len(value) >= _MIN_LITERAL_LEN:
            out.append(Literal(value, weight, f"attr:{lname}"))
        if lname in ("src", "href", "srcset"):
            base = _basename(value)
            if base and base != value and len(base) >= _MIN_LITERAL_LEN:
                out.append(Literal(base, _SRC_BASENAME_WEIGHT, "src-basename"))
                stem = _stem(base)
                if stem and stem != base and len(stem) >= _MIN_LITERAL_LEN:
                    out.append(Literal(stem, _SRC_STEM_WEIGHT, "src-stem"))
        if lname in _PASSTHROUGH_PROPS:
            out.append(Literal(f" {lname}=", _PASSTHROUGH_PROPS[lname], _PROP_KIND))

    text = " ".join(_WS.sub(" ", t).strip() for t in _TEXT.findall(html))
    text = htmlmod.unescape(text).strip()
    if _MIN_LITERAL_LEN <= len(text) <= _MAX_TEXT_LEN:
        out.append(Literal(text, _TEXT_WEIGHT, "text"))

    # Dedupe by value, keep the heaviest.
    best: dict[str, Literal] = {}
    for lit in out:
        if lit.value not in best or best[lit.value].weight < lit.weight:
            best[lit.value] = lit
    return ElementLiterals(tag=tag, literals=sorted(best.values(), key=lambda x: -x.weight))


_HASH_SEGMENT = re.compile(r"[.\-_][a-f0-9]{6,}(?=\.|$)", re.IGNORECASE)


def _stem(basename: str) -> str | None:
    """'hero-mountains.8f3a2c1d.jpg' -> 'hero-mountains'; bundlers rewrite the
    hash and extension, the stem is what appears in the import."""
    name = basename.rsplit(".", 1)[0] if "." in basename else basename
    name = _HASH_SEGMENT.sub("", name)
    return name or None


def _basename(url: str) -> str | None:
    path = urlsplit(url).path if "://" in url or url.startswith("/") else url
    if path.startswith("data:"):
        return None
    name = path.rstrip("/").rsplit("/", 1)[-1]
    # Next.js image optimizer / hashed asset names carry no signal.
    if not name or re.fullmatch(r"[a-f0-9]{8,}(\.\w+)?", name):
        return None
    return name
