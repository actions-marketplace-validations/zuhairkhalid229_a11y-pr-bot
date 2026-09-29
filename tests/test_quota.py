"""Plan limits. Pure, so every boundary is cheap to pin."""

from datetime import UTC

import pytest

from app.store.quota import DEFAULT_PLAN, PLANS, check, limits_for, usage_id


def test_usage_id_is_installation_and_month():
    from datetime import datetime

    assert usage_id(555, datetime(2026, 9, 29, tzinfo=UTC)) == "555_202609"
    assert usage_id(555, datetime(2027, 1, 1, tzinfo=UTC)) == "555_202701"


def test_public_repos_bypass_every_limit():
    d = check(
        plan="free",
        repo_private=False,
        repo_id=7,
        usage={"private_scans_run": 10**6, "private_repo_ids": list(range(100))},
    )
    assert d.allowed and d.metered is False and d.message is None


def test_free_allows_first_private_repo():
    d = check(plan="free", repo_private=True, repo_id=7, usage=None)
    assert d.allowed and d.metered is True


def test_free_rejects_a_second_private_repo():
    d = check(plan="free", repo_private=True, repo_id=8, usage={"private_repo_ids": [7]})
    assert not d.allowed and d.reason == "private_repo_limit"
    assert "1 private repository" in d.message and "Public repositories are always unlimited" in d.message


def test_already_counted_repo_is_not_a_new_repo():
    d = check(
        plan="free", repo_private=True, repo_id=7, usage={"private_repo_ids": [7], "private_scans_run": 49}
    )
    assert d.allowed, "the one allowed repo may keep scanning"


@pytest.mark.parametrize("run,allowed", [(49, True), (50, False), (51, False)])
def test_monthly_scan_boundary(run, allowed):
    d = check(
        plan="free", repo_private=True, repo_id=7, usage={"private_repo_ids": [7], "private_scans_run": run}
    )
    assert d.allowed is allowed
    if not allowed:
        assert d.reason == "scan_limit" and f"{run}/50" in d.message


def test_repo_limit_is_checked_before_scan_limit():
    """A brand-new repo over both limits should hear about the repo cap, which
    is the one an upgrade actually fixes for them today."""
    d = check(
        plan="free", repo_private=True, repo_id=99, usage={"private_repo_ids": [7], "private_scans_run": 500}
    )
    assert d.reason == "private_repo_limit"


def test_pro_and_team_ceilings():
    assert check(
        plan="pro",
        repo_private=True,
        repo_id=9,
        usage={"private_repo_ids": [1, 2, 3, 4], "private_scans_run": 499},
    ).allowed
    assert not check(
        plan="pro",
        repo_private=True,
        repo_id=9,
        usage={"private_repo_ids": [1, 2, 3, 4, 5], "private_scans_run": 0},
    ).allowed
    unlimited = check(
        plan="team",
        repo_private=True,
        repo_id=9,
        usage={"private_repo_ids": list(range(99)), "private_scans_run": 10**6},
    )
    assert unlimited.allowed and unlimited.metered is True


def test_unknown_and_missing_plan_fall_back_to_free():
    assert limits_for(None).name == DEFAULT_PLAN
    assert limits_for("enterprise-lol").name == DEFAULT_PLAN
    assert PLANS["free"].private_repos == 1 and PLANS["free"].scans_per_month == 50


def test_string_repo_ids_from_firestore_are_coerced():
    """Firestore can hand back numbers as strings after a JSON round trip."""
    d = check(plan="free", repo_private=True, repo_id=7, usage={"private_repo_ids": ["7"]})
    assert d.allowed
