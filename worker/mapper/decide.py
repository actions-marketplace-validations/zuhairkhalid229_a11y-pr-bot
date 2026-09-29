"""Stage 4: turn a verified mapping into a posting decision.

    suggestion   location >= 0.80 AND patch >= 0.80 AND not requires_human
                 AND original verified in file AND span inside diff hunks
                 AND replacement structurally sound AND no placeholder text
    annotation   location >= 0.60 AND original verified (patch shown as a
                 code block, not a suggestion fence)
    drop         anything else; reason recorded

The model's confidences are inputs, not verdicts. Every AND above is a check
we performed ourselves.
"""

from __future__ import annotations

from dataclasses import dataclass

SUGGEST_LOCATION_MIN = 0.80
SUGGEST_PATCH_MIN = 0.80
ANNOTATE_LOCATION_MIN = 0.60


@dataclass(frozen=True)
class Decision:
    disposition: str  # suggestion | annotation | drop
    reason: str


def decide(
    *,
    location_confidence: float,
    patch_confidence: float,
    requires_human_content: bool,
    original_verified: bool,
    in_diff: bool,
    structurally_sound: bool,
    placeholder: bool,
    has_patch: bool,
) -> Decision:
    if not original_verified:
        return Decision("drop", "original text not found in file")
    if location_confidence < ANNOTATE_LOCATION_MIN:
        return Decision("drop", f"location confidence {location_confidence:.2f} < {ANNOTATE_LOCATION_MIN}")

    if location_confidence < SUGGEST_LOCATION_MIN:
        return Decision(
            "annotation", f"location confidence {location_confidence:.2f} below suggestion threshold"
        )
    if not has_patch:
        return Decision("annotation", "no patch produced")
    if requires_human_content:
        return Decision("annotation", "fix needs human-supplied content")
    if placeholder:
        return Decision("annotation", "replacement contains placeholder text")
    if patch_confidence < SUGGEST_PATCH_MIN:
        return Decision("annotation", f"patch confidence {patch_confidence:.2f} < {SUGGEST_PATCH_MIN}")
    if not structurally_sound:
        return Decision("annotation", "replacement fails structural balance check")
    if not in_diff:
        return Decision("annotation", "lines not inside the PR diff hunks")
    return Decision("suggestion", "all checks passed")
