"""The smoke test's own logic. It is a diagnostic tool, so the thing worth
testing is that it fails loudly with the right guidance rather than silently."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "smoke_test", Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"
)
smoke = importlib.util.module_from_spec(spec)
sys.modules["smoke_test"] = smoke
spec.loader.exec_module(smoke)


def test_fixture_contains_exactly_one_deliberate_violation():
    html = smoke.FIXTURE
    assert "<img" in html and "alt=" not in html, "the img must have no alt"
    assert 'lang="en"' in html, "a missing lang would add a second unrelated finding"
    assert "<title>" in html, "a missing title would add a third"


def test_stage_failure_carries_actionable_hints():
    exc = smoke.StageFailure("boom", ["check the thing", "then the other thing"])
    assert str(exc) == "boom" and len(exc.hints) == 2


async def test_poll_returns_first_truthy_value():
    ctx = smoke.Context(repo="a/b", timeout=5)
    s = object.__new__(smoke.Smoke)
    s.ctx = ctx
    calls = []

    async def predicate():
        calls.append(1)
        return "ready" if len(calls) >= 3 else None

    got = await smoke.Smoke._poll(s, predicate, what="x", timeout=5, hints=[], interval=0.01)
    assert got == "ready" and len(calls) == 3


async def test_poll_raises_stage_failure_with_hints_on_timeout():
    s = object.__new__(smoke.Smoke)
    s.ctx = smoke.Context(repo="a/b", timeout=1)

    async def never():
        return None

    with pytest.raises(smoke.StageFailure) as exc:
        await smoke.Smoke._poll(
            s, never, what="a preview", timeout=0.05, hints=["check Deployments permission"], interval=0.01
        )
    assert "a preview" in str(exc.value)
    assert exc.value.hints == ["check Deployments permission"]


async def test_poll_propagates_a_stage_failure_raised_inside_the_predicate():
    """no_preview must abort immediately, not wait out the whole timeout."""
    s = object.__new__(smoke.Smoke)
    s.ctx = smoke.Context(repo="a/b", timeout=999)

    async def gave_up():
        raise smoke.StageFailure("gave up waiting", ["Deployments: read missing"])

    with pytest.raises(smoke.StageFailure, match="gave up waiting"):
        await smoke.Smoke._poll(s, gave_up, what="x", timeout=999, hints=[], interval=0.01)


def test_branch_name_is_unique_per_run():
    assert smoke.BRANCH.startswith("a11y-smoke-")
    assert smoke.BRANCH[len("a11y-smoke-") :].isdigit()


def test_scan_id_matches_the_pipeline_convention():
    """If this drifts from app.store.firestore, every poll looks at the wrong doc."""
    from app.store.firestore import scan_id_for

    assert scan_id_for(555, "acme/site", "a" * 40) == f"555_acme__site_{'a' * 40}"
