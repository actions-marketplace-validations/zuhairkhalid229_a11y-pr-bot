"""The finding schema. Every later day writes into this; nothing replaces it.

Lifecycle of one Finding
------------------------
  Day 2  scanner fills identity + evidence: rule, wcag, en_301_549, selector,
         html, failure_summary, page_path, fingerprint.
  Day 4  source-mapper fills `source` (file/line/confidence) and `patch`.
  Day 5  poster fills `posted` and uses `fingerprint` to decide what to skip,
         re-post or resolve on the next push.

Fingerprint design
------------------
The fingerprint must survive the things that legitimately change between two
scans of the same PR while catching the things that mean "this is a different
problem":

  survives   a new preview host per deploy       -> hash the path, never the host
  survives   a sibling element being inserted    -> hash the element, not the
                                                    nth-child selector path
  survives   CSS-in-JS / useId re-hashing        -> strip generated ids/classes
  changes    the developer fixed the element     -> normalised html differs
  changes    a different rule fires on it        -> rule_id is in the hash

`file` is deliberately NOT part of the fingerprint. It does not exist until
Day 4 and it is derived from the rendered evidence, so putting it in the hash
would make the fingerprint depend on the mapper's confidence that day.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

SCHEMA_VERSION = 2


class Impact(str, Enum):
    minor = "minor"
    moderate = "moderate"
    serious = "serious"
    critical = "critical"


class WcagCriterion(BaseModel):
    """One WCAG success criterion, e.g. 1.1.1 Non-text Content (A, since 2.0)."""

    id: str = Field(examples=["1.4.3"])
    name: str
    level: Literal["A", "AA", "AAA"]
    introduced_in: Literal["2.0", "2.1", "2.2"]
    # 4.1.1 Parsing was removed in WCAG 2.2. axe still tags some rules with it;
    # we keep the mapping so nothing crashes, but flag it for the report layer.
    obsolete_in_2_2: bool = False


class SourceLocation(BaseModel):
    """Where in the PR the rendered element came from. Day 4."""

    file: str
    line_start: int
    line_end: int
    confidence: float = Field(ge=0.0, le=1.0)
    # exact: data-source attribute / sourcemap. grep: literal match on
    # class/text. llm: Gemini correlated it. Posting rules differ per method.
    method: Literal["exact", "grep", "llm"]
    in_diff: bool = False


class Patch(BaseModel):
    """A proposed fix. Day 4. Becomes a ```suggestion block on Day 5."""

    original: str
    replacement: str
    rationale: str
    generated_by: str = Field(description="model id, for audit")
    confidence: float = Field(
        ge=0.0, le=1.0, description="P(committing as-is fixes it without breaking code)"
    )
    # True when the right fix needs facts the model cannot know (what an image
    # shows, what a button does). Never a one-click suggestion.
    requires_human_content: bool = False


Disposition = Literal["suggestion", "annotation", "drop", "duplicate"]


class PostedRef(BaseModel):
    """What we told GitHub about this finding. Day 5."""

    kind: Literal["suggestion", "annotation"]
    comment_id: int | None = None
    posted_at: datetime


class Finding(BaseModel):
    schema_version: int = SCHEMA_VERSION

    # -- identity ---------------------------------------------------------
    fingerprint: str
    rule_id: str = Field(examples=["image-alt"])
    impact: Impact

    # -- standards --------------------------------------------------------
    wcag: list[WcagCriterion]
    # EN 301 549 clause ids straight from axe's tags, e.g. "9.1.1.1". This is
    # the EAA's harmonised standard; it feeds the conformance export directly.
    en_301_549: list[str] = Field(default_factory=list)

    # -- evidence (rendered DOM) -------------------------------------------
    page_url: str
    page_path: str
    selector: str = Field(description="axe target selector, rendered DOM")
    html: str = Field(description="outerHTML, truncated")
    failure_summary: str
    help: str
    help_url: str

    # -- filled by later days --------------------------------------------
    source: SourceLocation | None = None
    patch: Patch | None = None
    # How Day 5 should post this, decided by worker/mapper/decide.py.
    disposition: Disposition | None = None
    disposition_reason: str | None = None
    # For "duplicate": fingerprint of the finding that carries the patch.
    duplicate_of: str | None = None
    posted: PostedRef | None = None


class ScanStatus(str, Enum):
    ok = "ok"
    # The URL is wrong, gone, or refuses us. Retrying will not help; the scan is
    # over and the Check Run should say why.
    failed_permanent = "failed_permanent"
    # Timeout, connection reset, browser crash. Cloud Tasks should retry.
    failed_transient = "failed_transient"


class ScanResult(BaseModel):
    schema_version: int = SCHEMA_VERSION
    scan_id: str
    status: ScanStatus
    error: str | None = None

    requested_url: str
    final_url: str | None = None
    page_title: str | None = None

    engine: Literal["axe-core"] = "axe-core"
    engine_version: str
    mode: Literal["browser", "static"] = "browser"
    viewport: str = "1280x800"

    started_at: datetime
    finished_at: datetime
    duration_ms: int

    # axe "violations": definite failures.
    findings: list[Finding] = Field(default_factory=list)
    # axe "incomplete": rules that could not decide automatically (e.g. contrast
    # over a background image, alt text that exists but may be meaningless).
    # This list is the input to the Gemini judgment layer, not noise.
    needs_review: list[Finding] = Field(default_factory=list)

    passes: int = 0
    inapplicable: int = 0
    counts_by_impact: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
# React useId (":r1:", ":R2m:"), MUI (":r0:"), headless-ui ("headlessui-menu-button-:r3:")
_GENERATED_ID = re.compile(r'\bid="[^"]*:[rR][0-9a-z]+:[^"]*"')
# emotion "css-1a2b3c", styled-components "sc-AxjAm", CSS modules "Button_root__x8Kz1",
# stylex "x1a2b3c4", Linaria "_1a2b3c"
_GENERATED_CLASS = re.compile(
    r"\b(?:css-[a-z0-9]+|sc-[A-Za-z0-9]+|[A-Za-z]+_[A-Za-z]+__[A-Za-z0-9]{5,}|x[0-9a-z]{6,}|_[0-9a-z]{6,})\b"
)
_STYLE_ATTR = re.compile(r'\sstyle="[^"]*"')
_DATA_REACT = re.compile(r'\sdata-react[a-z-]*="[^"]*"')
_HTML_TRUNCATE = 500


def normalise_html(html: str) -> str:
    """Strip everything that changes between builds without changing meaning."""
    out = _STYLE_ATTR.sub("", html)
    out = _DATA_REACT.sub("", out)
    out = _GENERATED_ID.sub("", out)
    out = _GENERATED_CLASS.sub("", out)
    out = _WS.sub(" ", out).strip()
    return out[:_HTML_TRUNCATE]


def page_path_of(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def compute_fingerprint(rule_id: str, page_path: str, html: str) -> str:
    material = "\x1f".join([rule_id, page_path, normalise_html(html)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
