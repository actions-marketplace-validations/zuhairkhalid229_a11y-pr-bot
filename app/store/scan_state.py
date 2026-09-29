"""The scan state machine as pure functions.

Nothing here touches Firestore. `apply_half` takes the document as it is and
one half (PR or preview) and returns the document as it should be plus whether
the caller has just won the right to enqueue. The transactional wrapper in
firestore.py calls it inside a transaction; the tests call it directly.

    (absent) ──pr──▶ awaiting_deployment ──preview──▶ queued
    (absent) ──preview──▶ awaiting_pr ──pr──▶ queued
    queued ──▶ in_progress ──▶ completed | failed
    awaiting_deployment ──fallback @15m──▶ no_preview
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ScanState(str, Enum):
    awaiting_deployment = "awaiting_deployment"
    awaiting_pr = "awaiting_pr"
    queued = "queued"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    no_preview = "no_preview"
    quota_exceeded = "quota_exceeded"


# Once here, a late-arriving half or a redeploy of the same sha changes nothing.
STICKY = frozenset(
    {
        ScanState.queued,
        ScanState.in_progress,
        ScanState.completed,
        ScanState.failed,
        ScanState.no_preview,
        ScanState.quota_exceeded,
    }
)
PRE_QUEUE = frozenset({ScanState.awaiting_deployment, ScanState.awaiting_pr})


@dataclass(frozen=True)
class Merge:
    doc: dict[str, Any]
    should_enqueue: bool
    # True when this call created the PR half for the first time -- the caller
    # owns creating the Check Run and, if still waiting, the fallback task.
    pr_is_new: bool


def apply_half(
    existing: dict[str, Any] | None, *, pr: dict | None = None, preview: dict | None = None
) -> Merge:
    if (pr is None) == (preview is None):
        raise ValueError("exactly one of pr / preview must be given")

    doc: dict[str, Any] = dict(existing or {})
    state = ScanState(doc["state"]) if "state" in doc else None
    pr_is_new = pr is not None and "pr" not in doc

    if pr is not None:
        doc.setdefault("pr", pr)  # first PR event wins; synchronize is a new sha anyway
    if preview is not None and "preview" not in doc:
        doc["preview"] = preview

    if state in STICKY:
        return Merge(doc=doc, should_enqueue=False, pr_is_new=pr_is_new)

    has_both = "pr" in doc and "preview" in doc
    if has_both:
        doc["state"] = ScanState.queued.value
        return Merge(doc=doc, should_enqueue=True, pr_is_new=pr_is_new)

    doc["state"] = ScanState.awaiting_deployment.value if "pr" in doc else ScanState.awaiting_pr.value
    return Merge(doc=doc, should_enqueue=False, pr_is_new=pr_is_new)


def transition(
    existing: dict[str, Any] | None, *, allowed_from: set[ScanState], to: ScanState
) -> dict[str, Any] | None:
    """Return the updated doc if the move is legal, else None (no-op)."""
    if not existing or "state" not in existing:
        return None
    if ScanState(existing["state"]) not in allowed_from:
        return None
    return {**existing, "state": to.value}
