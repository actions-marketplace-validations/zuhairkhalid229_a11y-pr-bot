"""Check Run lifecycle.

The Check Run is the user-visible contract: it appears on the PR within seconds
of opening, before any scan has run, so the developer knows the bot saw them.
Day 3 moves it to in_progress once a preview deployment lands; Day 5 completes
it with the findings summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from app.config import Settings
from app.github.client import GitHubClient
from app.logging_config import get_logger

log = get_logger(__name__)

QUEUED_SUMMARY = (
    "Waiting for a preview deployment to scan.\n\n"
    "This check runs automatically once Vercel or Netlify reports a successful "
    "deployment for this commit. If no preview appears within 15 minutes, a "
    "static-only analysis of the changed files runs instead."
)


class ChecksAPI:
    def __init__(self, settings: Settings, client: GitHubClient) -> None:
        self._settings = settings
        self._client = client

    async def create_queued(self, *, installation_id: int, repo_full_name: str, head_sha: str) -> int | None:
        """Create the placeholder Check Run. Returns its id, or None on failure."""
        response = await self._client.request(
            "POST",
            f"/repos/{repo_full_name}/check-runs",
            installation_id,
            json={
                "name": self._settings.check_run_name,
                "head_sha": head_sha,
                "status": "queued",
                "output": {
                    "title": "Queued",
                    "summary": QUEUED_SUMMARY,
                },
            },
        )
        if response.status_code != 201:
            log.error(
                "check_run_create_failed",
                repo=repo_full_name,
                head_sha=head_sha,
                status=response.status_code,
                body=response.text[:500],
            )
            return None

        check_run_id = response.json()["id"]
        log.info(
            "check_run_created",
            repo=repo_full_name,
            head_sha=head_sha,
            check_run_id=check_run_id,
        )
        return check_run_id

    async def start(self, *, installation_id: int, repo_full_name: str, check_run_id: int) -> bool:
        return await self.update(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            check_run_id=check_run_id,
            payload={
                "status": "in_progress",
                "started_at": _now_iso(),
                "output": {
                    "title": "Scanning preview deployment",
                    "summary": "Running axe-core against the preview…",
                },
            },
        )

    async def complete(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        check_run_id: int,
        conclusion: Literal["success", "neutral", "failure", "action_required"],
        title: str,
        summary: str,
        text: str | None = None,
        annotations: list[dict[str, Any]] | None = None,
    ) -> bool:
        output: dict[str, Any] = {"title": title[:255], "summary": summary[:65000]}
        if text:
            output["text"] = text[:65000]
        if annotations:
            # 50 per request is the API cap; further batches would need extra
            # PATCHes with the same output. One batch is enough for our cap.
            output["annotations"] = annotations[:50]
        return await self.update(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            check_run_id=check_run_id,
            payload={
                "status": "completed",
                "conclusion": conclusion,
                "completed_at": _now_iso(),
                "output": output,
            },
        )

    async def update(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        check_run_id: int,
        payload: dict[str, Any],
    ) -> bool:
        response = await self._client.request(
            "PATCH",
            f"/repos/{repo_full_name}/check-runs/{check_run_id}",
            installation_id,
            json=payload,
        )
        if response.status_code != 200:
            log.error(
                "check_run_update_failed",
                repo=repo_full_name,
                check_run_id=check_run_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return False
        return True


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
