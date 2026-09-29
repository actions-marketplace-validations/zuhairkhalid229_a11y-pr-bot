"""axe tag vocabulary -> WcagCriterion / EN 301 549 clauses.

axe tags a rule like:  ["cat.text-alternatives", "wcag2a", "wcag111",
                        "section508", "EN-301-549", "EN-9.1.1.1", "ACT"]

  wcag111    -> success criterion 1.1.1. Principle and guideline are always one
                digit (guidelines top out at 1.4, 2.5, 3.3, 4.1) so the split is
                unambiguous even for wcag1410 -> 1.4.10.
  wcag2a     -> the *level* (A/AA/AAA) is only on the version tag, never on the
                SC tag, so both are needed.
  EN-9.1.1.1 -> EN 301 549 clause. Passed through verbatim minus the prefix.
"""

from __future__ import annotations

import re

from worker.schema import WcagCriterion

_SC_TAG = re.compile(r"^wcag(\d)(\d)(\d{1,2})$")
_EN_TAG = re.compile(r"^EN-(9\.\d+\.\d+\.\d+)$")

# WCAG 2.2 Level A + AA. (id -> name, level, version introduced)
_CRITERIA: dict[str, tuple[str, str, str]] = {
    "1.1.1": ("Non-text Content", "A", "2.0"),
    "1.2.1": ("Audio-only and Video-only (Prerecorded)", "A", "2.0"),
    "1.2.2": ("Captions (Prerecorded)", "A", "2.0"),
    "1.2.3": ("Audio Description or Media Alternative (Prerecorded)", "A", "2.0"),
    "1.2.4": ("Captions (Live)", "AA", "2.0"),
    "1.2.5": ("Audio Description (Prerecorded)", "AA", "2.0"),
    "1.3.1": ("Info and Relationships", "A", "2.0"),
    "1.3.2": ("Meaningful Sequence", "A", "2.0"),
    "1.3.3": ("Sensory Characteristics", "A", "2.0"),
    "1.3.4": ("Orientation", "AA", "2.1"),
    "1.3.5": ("Identify Input Purpose", "AA", "2.1"),
    "1.4.1": ("Use of Color", "A", "2.0"),
    "1.4.2": ("Audio Control", "A", "2.0"),
    "1.4.3": ("Contrast (Minimum)", "AA", "2.0"),
    "1.4.4": ("Resize Text", "AA", "2.0"),
    "1.4.5": ("Images of Text", "AA", "2.0"),
    "1.4.10": ("Reflow", "AA", "2.1"),
    "1.4.11": ("Non-text Contrast", "AA", "2.1"),
    "1.4.12": ("Text Spacing", "AA", "2.1"),
    "1.4.13": ("Content on Hover or Focus", "AA", "2.1"),
    "2.1.1": ("Keyboard", "A", "2.0"),
    "2.1.2": ("No Keyboard Trap", "A", "2.0"),
    "2.1.4": ("Character Key Shortcuts", "A", "2.1"),
    "2.2.1": ("Timing Adjustable", "A", "2.0"),
    "2.2.2": ("Pause, Stop, Hide", "A", "2.0"),
    "2.3.1": ("Three Flashes or Below Threshold", "A", "2.0"),
    "2.4.1": ("Bypass Blocks", "A", "2.0"),
    "2.4.2": ("Page Titled", "A", "2.0"),
    "2.4.3": ("Focus Order", "A", "2.0"),
    "2.4.4": ("Link Purpose (In Context)", "A", "2.0"),
    "2.4.5": ("Multiple Ways", "AA", "2.0"),
    "2.4.6": ("Headings and Labels", "AA", "2.0"),
    "2.4.7": ("Focus Visible", "AA", "2.0"),
    "2.4.11": ("Focus Not Obscured (Minimum)", "AA", "2.2"),
    "2.5.1": ("Pointer Gestures", "A", "2.1"),
    "2.5.2": ("Pointer Cancellation", "A", "2.1"),
    "2.5.3": ("Label in Name", "A", "2.1"),
    "2.5.4": ("Motion Actuation", "A", "2.1"),
    "2.5.7": ("Dragging Movements", "AA", "2.2"),
    "2.5.8": ("Target Size (Minimum)", "AA", "2.2"),
    "3.1.1": ("Language of Page", "A", "2.0"),
    "3.1.2": ("Language of Parts", "AA", "2.0"),
    "3.2.1": ("On Focus", "A", "2.0"),
    "3.2.2": ("On Input", "A", "2.0"),
    "3.2.3": ("Consistent Navigation", "AA", "2.0"),
    "3.2.4": ("Consistent Identification", "AA", "2.0"),
    "3.2.6": ("Consistent Help", "A", "2.2"),
    "3.3.1": ("Error Identification", "A", "2.0"),
    "3.3.2": ("Labels or Instructions", "A", "2.0"),
    "3.3.3": ("Error Suggestion", "AA", "2.0"),
    "3.3.4": ("Error Prevention (Legal, Financial, Data)", "AA", "2.0"),
    "3.3.7": ("Redundant Entry", "A", "2.2"),
    "3.3.8": ("Accessible Authentication (Minimum)", "AA", "2.2"),
    "4.1.2": ("Name, Role, Value", "A", "2.0"),
    "4.1.3": ("Status Messages", "AA", "2.1"),
}

# Removed in WCAG 2.2. Kept so an axe tag never produces an unknown criterion.
_OBSOLETE: dict[str, tuple[str, str, str]] = {
    "4.1.1": ("Parsing", "A", "2.0"),
}

# axe runs these when we ask for A + AA conformance. AAA and best-practice are
# left out on purpose: the product's claim is "WCAG 2.2 AA", nothing broader.
RUN_ONLY_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def criteria_from_tags(tags: list[str]) -> list[WcagCriterion]:
    result: list[WcagCriterion] = []
    for tag in tags:
        match = _SC_TAG.match(tag)
        if not match:
            continue
        sc_id = ".".join(match.groups())
        if sc_id in _CRITERIA:
            name, level, version = _CRITERIA[sc_id]
            result.append(WcagCriterion(id=sc_id, name=name, level=level, introduced_in=version))
        elif sc_id in _OBSOLETE:
            name, level, version = _OBSOLETE[sc_id]
            result.append(
                WcagCriterion(
                    id=sc_id,
                    name=name,
                    level=level,
                    introduced_in=version,
                    obsolete_in_2_2=True,
                )
            )
        # AAA criteria are not in the table; with RUN_ONLY_TAGS they never
        # appear, and if a future axe adds one we drop it silently rather than
        # report a level we do not claim.
    return result


def en_clauses_from_tags(tags: list[str]) -> list[str]:
    return [m.group(1) for tag in tags if (m := _EN_TAG.match(tag))]
