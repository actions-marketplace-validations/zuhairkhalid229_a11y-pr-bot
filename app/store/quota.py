"""Plan limits as a pure function of the usage counters.

Reading of the free tier, which is a product decision as much as a technical
one: **public repositories are never metered.** They are the distribution
engine -- every PR comment on a public repo is an impression -- so capping them
would throttle the funnel to save pennies of Cloud Run CPU. The meter exists
for private repos only:

    free   public: unlimited
           private: 1 distinct repo, 50 scans / calendar month
    pro    private: 5 distinct repos, 500 scans / month
    team   unlimited

The distinct-repo check races (two private repos scanned in the same second can
both pass), which costs at most one extra private repo for one month. Making it
transactional would put a read-modify-write on the hot path of every scan for a
limit whose whole purpose is to nudge an upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class PlanLimits:
    name: str
    private_repos: int | None  # None = unlimited
    scans_per_month: int | None


PLANS: dict[str, PlanLimits] = {
    "free": PlanLimits("free", private_repos=1, scans_per_month=50),
    "pro": PlanLimits("pro", private_repos=5, scans_per_month=500),
    "team": PlanLimits("team", private_repos=None, scans_per_month=None),
}
DEFAULT_PLAN = "free"


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""
    # Shown on the Check Run; None when the scan is allowed.
    message: str | None = None
    metered: bool = False


def limits_for(plan: str | None) -> PlanLimits:
    return PLANS.get(plan or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def usage_id(installation_id: int, at: datetime | None = None) -> str:
    at = at or datetime.now(UTC)
    return f"{installation_id}_{at:%Y%m}"


def check(
    *,
    plan: str | None,
    repo_private: bool,
    repo_id: int,
    usage: dict[str, Any] | None,
) -> QuotaDecision:
    """Decide whether this scan may run. `usage` is the month's counter doc."""
    if not repo_private:
        return QuotaDecision(allowed=True, reason="public repo, not metered")

    limits = limits_for(plan)
    usage = usage or {}
    private_repos: list[int] = [int(r) for r in usage.get("private_repo_ids", [])]
    scans_run = int(usage.get("private_scans_run", 0))

    if limits.private_repos is not None:
        already = repo_id in private_repos
        if not already and len(private_repos) >= limits.private_repos:
            return QuotaDecision(
                allowed=False,
                reason="private_repo_limit",
                message=(
                    f"The **{limits.name}** plan covers {limits.private_repos} private "
                    f"repositor{'y' if limits.private_repos == 1 else 'ies'} and "
                    f"{len(private_repos)} {'is' if len(private_repos) == 1 else 'are'} "
                    "already in use this month. Public repositories are always unlimited. "
                    "Upgrade to scan this one."
                ),
                metered=True,
            )

    if limits.scans_per_month is not None and scans_run >= limits.scans_per_month:
        return QuotaDecision(
            allowed=False,
            reason="scan_limit",
            message=(
                f"Monthly private-repo scan limit reached ({scans_run}/"
                f"{limits.scans_per_month} on the **{limits.name}** plan). "
                "The counter resets on the 1st; public repositories keep scanning. "
                "Upgrade for a higher limit."
            ),
            metered=True,
        )

    return QuotaDecision(allowed=True, reason="within limits", metered=True)
