"""Post-queue lifecycle: everything that happens to a scan once Cloud Tasks
hands it to the worker, plus the fallback that fires if it never gets there.

    /tasks/scan      queued|in_progress ─▶ in_progress ─▶ completed | failed
    /tasks/fallback  awaiting_deployment ─▶ no_preview

Both are idempotent: every transition checks the current state inside a
Firestore transaction and becomes a no-op if the scan has moved on. That is
what makes Cloud Tasks retries and late fallbacks safe.

Retry accounting: Cloud Tasks sends X-CloudTasks-TaskRetryCount. Past
MAX_TRANSIENT_ATTEMPTS a transient failure is treated as final so the Check Run
never sits in in_progress forever.
"""

from __future__ import annotations

from typing import Any

from app.crypto import SecretBox
from app.github.checks import ChecksAPI
from app.github.client import GitHubClient
from app.logging_config import get_logger
from app.store import quota
from app.store.firestore import Store
from app.store.scan_state import ScanState
from worker import report
from worker.mapper.github_files import fetch_pr_files
from worker.mapper.mapper import MapStats, SourceMapper
from worker.poster.poster import Poster, PostOutcome
from worker.scanner import Scanner
from worker.schema import ScanResult, ScanStatus

log = get_logger(__name__)

MAX_TRANSIENT_ATTEMPTS = 4

NO_PREVIEW_SUMMARY = (
    "No successful preview deployment was reported for this commit within the "
    "wait window, so nothing was scanned.\n\n"
    "This usually means one of:\n"
    "- the repository is not connected to Vercel / Netlify / another provider "
    "that reports deployments to GitHub;\n"
    "- the deployment failed or was cancelled by a newer push;\n"
    "- the app does not have the **Deployments: read** permission.\n\n"
    "Push again once a preview deploys and the scan will run."
)


class Pipeline:
    def __init__(
        self,
        *,
        store: Store,
        checks: ChecksAPI,
        scanner: Scanner,
        secrets: SecretBox,
        github: GitHubClient | None = None,
        mapper: SourceMapper | None = None,
        poster: Poster | None = None,
    ) -> None:
        self._store = store
        self._checks = checks
        self._scanner = scanner
        self._secrets = secrets
        self._github = github
        self._mapper = mapper
        self._poster = poster

    # -- /tasks/scan ---------------------------------------------------------

    async def run_scan(
        self, scan_id: str, *, retry_count: int, extra_headers: dict[str, str] | None = None
    ) -> tuple[ScanResult | None, bool]:
        """Returns (result, retry). `retry` True means answer 503 to Cloud Tasks."""
        doc = await self._store.get_scan(scan_id)
        if doc is None:
            log.warning("scan_missing", scan_id=scan_id)
            return None, False

        moved = await self._store.transition_scan(
            scan_id,
            allowed_from={ScanState.queued, ScanState.in_progress},
            to=ScanState.in_progress,
            extra={"attempts": retry_count + 1},
        )
        if not moved:
            log.info("scan_not_runnable", scan_id=scan_id, state=doc.get("state"))
            return None, False

        ids = _ids(doc)

        # Quota gate. After the in_progress transition so the check is done
        # once per scan rather than once per Cloud Tasks retry, and before any
        # browser is launched -- an over-quota scan must cost nothing.
        decision = await self._check_quota(doc)
        if not decision.allowed:
            await self._retire_over_quota(scan_id, doc, decision)
            return None, False

        if retry_count == 0 and ids["check_run_id"]:
            await self._checks.start(**ids)

        headers = dict(extra_headers or {})
        headers.update(await self._provider_headers(doc))

        result = await self._scanner.scan(scan_id, doc["preview"]["url"], extra_headers=headers)

        if result.status is ScanStatus.failed_transient and retry_count + 1 < MAX_TRANSIENT_ATTEMPTS:
            # Leave the scan in in_progress; Cloud Tasks will call again.
            await self._store.update_scan(scan_id, {"last_error": result.error})
            return result, True

        await self._finish(scan_id, doc, result, exhausted=result.status is ScanStatus.failed_transient)
        return result, False

    async def _finish(
        self, scan_id: str, doc: dict[str, Any], result: ScanResult, *, exhausted: bool
    ) -> None:
        map_stats: MapStats | None = None
        post: PostOutcome | None = None
        if result.status is ScanStatus.ok and result.findings:
            map_stats = await self._map_sources(doc, result)
            post = await self._post(doc, result)

        if result.status is ScanStatus.ok:
            await self._store.write_findings(
                scan_id,
                [f.model_dump(mode="json") for f in result.findings + result.needs_review],
                installation_id=doc["installation_id"],
            )

        summary = {
            "status": result.status.value,
            "final_url": result.final_url,
            "page_title": result.page_title,
            "findings": len(result.findings),
            "needs_review": len(result.needs_review),
            "counts_by_impact": result.counts_by_impact,
            "passes": result.passes,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "engine_version": result.engine_version,
            "finished_at": result.finished_at,
            "mapping": _stats_dict(map_stats),
            "posting": post.summary() if post else None,
        }
        to = ScanState.completed if result.status is ScanStatus.ok else ScanState.failed
        await self._store.transition_scan(
            scan_id, allowed_from={ScanState.in_progress}, to=to, extra={"result": summary}
        )

        ids = _ids(doc)
        if ids["check_run_id"]:
            if exhausted:
                error = f"{result.error} (gave up after {MAX_TRANSIENT_ATTEMPTS} attempts)"
                result = result.model_copy(update={"error": error})
            await self._checks.complete(
                **ids,
                conclusion=report.conclusion_for(result),
                title=report.title_for(result),
                summary=report.summary_for(result, posting=post.summary() if post else None),
                text=report.text_for(result),
                annotations=post.check_annotations if post else None,
            )

    # -- quota ---------------------------------------------------------------

    async def _check_quota(self, doc: dict[str, Any]) -> quota.QuotaDecision:
        """Public repos short-circuit without touching Firestore."""
        if not doc.get("repo_private"):
            return quota.QuotaDecision(allowed=True, reason="public repo, not metered")

        installation_id = doc["installation_id"]
        uid = quota.usage_id(installation_id)
        plan = await self._store.get_plan(installation_id)
        decision = quota.check(
            plan=plan,
            repo_private=True,
            repo_id=doc["repo_id"],
            usage=await self._store.get_usage(uid),
        )
        if decision.allowed:
            await self._store.count_private_scan(uid, doc["repo_id"])
        log.info(
            "quota_checked",
            installation_id=installation_id,
            plan=plan or quota.DEFAULT_PLAN,
            allowed=decision.allowed,
            reason=decision.reason,
        )
        return decision

    async def _retire_over_quota(
        self, scan_id: str, doc: dict[str, Any], decision: quota.QuotaDecision
    ) -> None:
        await self._store.transition_scan(
            scan_id,
            allowed_from={ScanState.in_progress},
            to=ScanState.quota_exceeded,
            extra={"quota_reason": decision.reason},
        )
        ids = _ids(doc)
        if ids["check_run_id"]:
            await self._checks.complete(
                **ids,
                conclusion="neutral",
                title="Scan skipped — plan limit reached",
                summary=decision.message or "Plan limit reached.",
            )
        log.info("scan_retired_over_quota", scan_id=scan_id, reason=decision.reason)

    async def _post(self, doc: dict[str, Any], result: ScanResult) -> PostOutcome | None:
        """Day 5: review comments + fixed-thread replies. Same rule as mapping:
        must never fail the scan."""
        if self._poster is None or not doc.get("pr"):
            return None
        try:
            return await self._poster.post(doc, result)
        except Exception:
            log.exception("posting_failed", repo=doc.get("repo_full_name"))
            return None

    async def _map_sources(self, doc: dict[str, Any], result: ScanResult) -> MapStats | None:
        """Day 4: locate each finding in the PR's changed files and draft a
        patch. Failure here must never fail the scan -- findings without a
        source are still reported on the Check Run."""
        if self._mapper is None or self._github is None or not doc.get("pr"):
            for f in result.findings:
                f.disposition, f.disposition_reason = "drop", "source mapping disabled"
            return None
        try:
            pr_files = await fetch_pr_files(
                self._github,
                installation_id=doc["installation_id"],
                repo_full_name=doc["repo_full_name"],
                pr_number=doc["pr"]["number"],
                head_sha=doc["head_sha"],
            )
            return await self._mapper.map_findings(result.findings, pr_files)
        except Exception:
            log.exception("source_mapping_failed", repo=doc.get("repo_full_name"))
            for f in result.findings:
                if f.disposition is None:
                    f.disposition, f.disposition_reason = "drop", "mapping error"
            return None

    # -- /tasks/fallback -----------------------------------------------------

    async def run_fallback(self, scan_id: str) -> bool:
        """True if this call retired the scan; False if it had already moved on."""
        doc = await self._store.get_scan(scan_id)
        if doc is None:
            return False

        moved = await self._store.transition_scan(
            scan_id, allowed_from={ScanState.awaiting_deployment}, to=ScanState.no_preview
        )
        if not moved:
            log.info("fallback_noop", scan_id=scan_id, state=doc.get("state"))
            return False

        ids = _ids(doc)
        if ids["check_run_id"]:
            await self._checks.complete(
                **ids,
                conclusion="neutral",
                title="No preview deployment found",
                summary=NO_PREVIEW_SUMMARY,
            )
        log.info("scan_retired_no_preview", scan_id=scan_id)
        return True

    # -----------------------------------------------------------------------

    async def _provider_headers(self, doc: dict[str, Any]) -> dict[str, str]:
        """Resolve per-repo provider secrets. Never logged, never in the task
        payload -- looked up here, used once, dropped."""
        config = await self._store.get_repo_config(doc["installation_id"], doc["repo_id"])
        if not config:
            return {}
        headers: dict[str, str] = {}
        if token := config.get("vercel_bypass_secret"):
            plaintext = self._secrets.decrypt(token)
            if plaintext:
                headers["x-vercel-protection-bypass"] = plaintext
            else:
                log.warning("bypass_secret_undecryptable", repo_id=doc["repo_id"])
        return headers


def _stats_dict(stats: MapStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return dict(stats.__dict__)


def _ids(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "installation_id": doc["installation_id"],
        "repo_full_name": doc["repo_full_name"],
        "check_run_id": doc.get("check_run_id"),
    }
