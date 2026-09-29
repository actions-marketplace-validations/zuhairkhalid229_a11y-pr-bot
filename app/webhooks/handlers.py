"""Event handlers.

Everything here runs *after* the 202 has been sent, via BackgroundTasks. See the
note in main.py about Cloud Run CPU throttling -- the api service must be
deployed with --no-cpu-throttling or this code is frozen mid-flight.

The PR and deployment handlers are two halves of one state machine; see
app/store/scan_state.py. Each registers its half transactionally and acts only
on what the merge tells it: create the Check Run if the PR half is new, enqueue
the scan if it just completed the pair, schedule the fallback if it is still
waiting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.github.checks import ChecksAPI
from app.logging_config import get_logger
from app.queue.tasks import TaskQueue
from app.store.firestore import Store, scan_id_for
from app.webhooks.deployments import extract_preview

log = get_logger(__name__)

# Reopened is deliberately included: a PR reopened months later deserves a fresh
# scan against current rules. Edited/labeled are not -- they do not change code.
PR_SCAN_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class EventHandlers:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        checks: ChecksAPI,
        queue: TaskQueue,
    ) -> None:
        self._settings = settings
        self._store = store
        self._checks = checks
        self._queue = queue

    async def dispatch(self, event: str, payload: dict[str, Any]) -> None:
        handler = {
            "pull_request": self.handle_pull_request,
            "deployment_status": self.handle_deployment_status,
            "installation": self.handle_installation,
            "installation_repositories": self.handle_installation,
        }.get(event)

        if handler is None:
            log.info("event_ignored", gh_event=event, action=payload.get("action"))
            return

        try:
            await handler(payload)
        except Exception:
            # Never raise past here: the 202 is already sent and an unhandled
            # exception in a background task takes down nothing useful.
            log.exception("handler_failed", gh_event=event, action=payload.get("action"))

    # -- pull_request: the PR half ------------------------------------------

    async def handle_pull_request(self, payload: dict[str, Any]) -> None:
        action = payload.get("action")
        if action not in PR_SCAN_ACTIONS:
            log.info("pr_action_ignored", action=action)
            return

        pull_request = payload["pull_request"]
        if pull_request.get("draft") and action != "ready_for_review":
            log.info("draft_pr_skipped", number=pull_request["number"])
            return

        installation_id = payload["installation"]["id"]
        repo = payload["repository"]
        head_sha = pull_request["head"]["sha"]
        scan_id = scan_id_for(installation_id, repo["full_name"], head_sha)

        merge = await self._store.register_half(
            scan_id,
            base=_base(installation_id, repo, head_sha),
            pr={
                "number": pull_request["number"],
                "head_ref": pull_request["head"]["ref"],
                "base_ref": pull_request["base"]["ref"],
                "seen_at": _now(),
            },
        )

        check_run_id: int | None = merge.doc.get("check_run_id")
        if merge.pr_is_new:
            check_run_id = await self._checks.create_queued(
                installation_id=installation_id,
                repo_full_name=repo["full_name"],
                head_sha=head_sha,
            )
            await self._store.update_scan(scan_id, {"check_run_id": check_run_id})

        if merge.should_enqueue:
            await self._enqueue(scan_id, merge.doc, check_run_id)
        elif merge.pr_is_new:
            task = await self._queue.enqueue_fallback(scan_id)
            await self._store.update_scan(scan_id, {"fallback_task_name": task})

        log.info(
            "pull_request_registered",
            scan_id=scan_id,
            state=merge.doc.get("state"),
            enqueued=merge.should_enqueue,
        )

    # -- deployment_status: the preview half --------------------------------

    async def handle_deployment_status(self, payload: dict[str, Any]) -> None:
        preview = extract_preview(payload)
        if preview is None:
            log.info(
                "deployment_ignored",
                state=(payload.get("deployment_status") or {}).get("state"),
                environment=(payload.get("deployment") or {}).get("environment"),
            )
            return

        installation_id = payload["installation"]["id"]
        repo = payload["repository"]
        scan_id = scan_id_for(installation_id, repo["full_name"], preview.sha)

        merge = await self._store.register_half(
            scan_id,
            base=_base(installation_id, repo, preview.sha),
            preview={
                "url": preview.url,
                "provider": preview.provider,
                "environment": preview.environment,
                "deployment_id": preview.deployment_id,
                "seen_at": _now(),
            },
        )

        if merge.should_enqueue:
            await self._enqueue(scan_id, merge.doc, merge.doc.get("check_run_id"))

        log.info(
            "deployment_registered",
            scan_id=scan_id,
            provider=preview.provider,
            state=merge.doc.get("state"),
            enqueued=merge.should_enqueue,
        )

    # -- installation ---------------------------------------------------------

    async def handle_installation(self, payload: dict[str, Any]) -> None:
        installation = payload["installation"]
        account = installation.get("account") or {}
        await self._store.record_installation(
            installation["id"],
            {
                "account_login": account.get("login"),
                "account_type": account.get("type"),
                "app_id": installation.get("app_id"),
                "repository_selection": installation.get("repository_selection"),
                "suspended": installation.get("suspended_at") is not None,
                "last_action": payload.get("action"),
                "plan": "free",
            },
        )
        log.info(
            "installation_recorded",
            installation_id=installation["id"],
            account=account.get("login"),
            action=payload.get("action"),
        )

    # -----------------------------------------------------------------------

    async def _enqueue(self, scan_id: str, doc: dict[str, Any], check_run_id: int | None) -> None:
        task = await self._queue.enqueue_scan(
            {
                "scan_id": scan_id,
                "url": doc["preview"]["url"],
                "installation_id": doc["installation_id"],
                "repo_id": doc["repo_id"],
                "repo_full_name": doc["repo_full_name"],
                "pr_number": doc["pr"]["number"],
                "head_sha": doc["head_sha"],
                "check_run_id": check_run_id,
            }
        )
        await self._store.update_scan(scan_id, {"scan_task_name": task, "queued_at": _now()})


def _base(installation_id: int, repo: dict[str, Any], head_sha: str) -> dict[str, Any]:
    return {
        "installation_id": installation_id,
        "repo_id": repo["id"],
        "repo_full_name": repo["full_name"],
        # Quota meters private repos only (app/store/quota.py), so this has to
        # travel with the scan -- the worker never sees the webhook payload.
        "repo_private": bool(repo.get("private", False)),
        "head_sha": head_sha,
    }


def _now() -> datetime:
    return datetime.now(UTC)
