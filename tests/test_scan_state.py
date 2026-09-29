"""Every legal path through the scan state machine, and the illegal ones."""

import pytest

from app.store.scan_state import ScanState, apply_half, transition

PR = {"number": 7, "head_ref": "feat", "base_ref": "main"}
PREVIEW = {"url": "https://x-abc.vercel.app", "provider": "vercel"}


def test_pr_first_waits_for_deployment():
    m = apply_half(None, pr=PR)
    assert m.doc["state"] == "awaiting_deployment"
    assert m.pr_is_new is True
    assert m.should_enqueue is False


def test_preview_first_waits_for_pr():
    m = apply_half(None, preview=PREVIEW)
    assert m.doc["state"] == "awaiting_pr"
    assert m.pr_is_new is False
    assert m.should_enqueue is False


def test_pr_then_preview_enqueues_once():
    first = apply_half(None, pr=PR)
    second = apply_half(first.doc, preview=PREVIEW)
    assert second.doc["state"] == "queued"
    assert second.should_enqueue is True
    assert second.pr_is_new is False


def test_preview_then_pr_enqueues_once_and_pr_is_new():
    first = apply_half(None, preview=PREVIEW)
    second = apply_half(first.doc, pr=PR)
    assert second.doc["state"] == "queued"
    assert second.should_enqueue is True
    assert second.pr_is_new is True, "caller must still create the Check Run"


@pytest.mark.parametrize("state", ["queued", "in_progress", "completed", "failed", "no_preview"])
def test_sticky_states_ignore_late_halves(state):
    doc = {"state": state, "pr": PR, "preview": PREVIEW}
    assert apply_half(doc, preview={"url": "https://redeploy.vercel.app"}).should_enqueue is False
    assert apply_half(doc, pr=PR).should_enqueue is False
    assert apply_half(doc, preview={"url": "x"}).doc["state"] == state
    # First preview wins; a redeploy of the same sha does not swap URLs mid-flight.
    assert apply_half(doc, preview={"url": "https://redeploy.vercel.app"}).doc["preview"] == PREVIEW


def test_duplicate_pr_delivery_is_idempotent():
    first = apply_half(None, pr=PR)
    again = apply_half(first.doc, pr=PR)
    assert again.pr_is_new is False
    assert again.should_enqueue is False
    assert again.doc["state"] == "awaiting_deployment"


def test_duplicate_preview_delivery_is_idempotent():
    first = apply_half(None, preview=PREVIEW)
    again = apply_half(first.doc, preview=PREVIEW)
    assert again.should_enqueue is False
    assert again.doc["state"] == "awaiting_pr"


def test_exactly_one_half_required():
    with pytest.raises(ValueError):
        apply_half(None)
    with pytest.raises(ValueError):
        apply_half(None, pr=PR, preview=PREVIEW)


def test_transition_legal_and_illegal():
    doc = {"state": "queued"}
    assert (
        transition(doc, allowed_from={ScanState.queued}, to=ScanState.in_progress)["state"] == "in_progress"
    )
    assert transition(doc, allowed_from={ScanState.in_progress}, to=ScanState.completed) is None
    assert transition(None, allowed_from={ScanState.queued}, to=ScanState.in_progress) is None
    assert transition({}, allowed_from={ScanState.queued}, to=ScanState.in_progress) is None


def test_fallback_only_retires_awaiting_deployment():
    ok = transition(
        {"state": "awaiting_deployment"},
        allowed_from={ScanState.awaiting_deployment},
        to=ScanState.no_preview,
    )
    assert ok["state"] == "no_preview"
    for state in ("awaiting_pr", "queued", "in_progress", "completed"):
        assert (
            transition(
                {"state": state}, allowed_from={ScanState.awaiting_deployment}, to=ScanState.no_preview
            )
            is None
        )
