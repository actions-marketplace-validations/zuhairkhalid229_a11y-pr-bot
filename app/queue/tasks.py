"""Cloud Tasks enqueue.

The webhook must ACK fast, so nothing that touches Playwright, Gemini or the
GitHub API beyond the Check Run happens inline. Tasks carry an OIDC token so the
worker service can stay private (`--no-allow-unauthenticated`).

Two task kinds, both idempotent on the worker side:
  scan      POST {worker}/tasks/scan      now
  fallback  POST {worker}/tasks/fallback  at +preview_wait_minutes; no-op unless
                                          the scan is still awaiting_deployment

There is no Cloud Tasks emulator; with ENABLE_CLOUD_TASKS=false the payload is
logged instead, which is what you want for the smee loop.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Cloud Tasks names allow only [A-Za-z0-9_-]; repo names routinely contain dots.
_TASK_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class TaskQueue:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: tasks_v2.CloudTasksAsyncClient | None = None

    def _get_client(self) -> tasks_v2.CloudTasksAsyncClient:
        if self._client is None:
            self._client = tasks_v2.CloudTasksAsyncClient()
        return self._client

    async def enqueue_scan(self, payload: dict[str, Any]) -> str | None:
        return await self._enqueue(
            name=payload["scan_id"],
            path="/tasks/scan",
            payload=payload,
        )

    async def enqueue_fallback(self, scan_id: str) -> str | None:
        delay = timedelta(minutes=self._settings.preview_wait_minutes)
        return await self._enqueue(
            name=f"{scan_id}-fallback",
            path="/tasks/fallback",
            payload={"scan_id": scan_id},
            schedule_at=datetime.now(UTC) + delay,
        )

    async def _enqueue(
        self,
        *,
        name: str,
        path: str,
        payload: dict[str, Any],
        schedule_at: datetime | None = None,
    ) -> str | None:
        url = f"{self._settings.worker_base_url.rstrip('/')}{path}"

        if not self._settings.enable_cloud_tasks:
            log.info(
                "task_enqueue_skipped",
                reason="disabled",
                url=url,
                schedule_at=schedule_at.isoformat() if schedule_at else None,
                payload=payload,
            )
            return None

        client = self._get_client()
        parent = client.queue_path(
            self._settings.gcp_project_id,
            self._settings.tasks_location,
            self._settings.tasks_queue,
        )
        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode("utf-8"),
                "oidc_token": {
                    "service_account_email": self._settings.tasks_invoker_sa,
                    "audience": url,
                },
            },
            # Queue-level dedupe: the same name within ~1h collapses to one task.
            "name": f"{parent}/tasks/{_TASK_NAME_SAFE.sub('-', name)}",
        }
        if schedule_at is not None:
            ts = timestamp_pb2.Timestamp()
            ts.FromDatetime(schedule_at)
            task["schedule_time"] = ts

        try:
            created = await client.create_task(request={"parent": parent, "task": task})
        except gcloud_exceptions.AlreadyExists:
            log.info("task_already_queued", name=name)
            return None
        log.info("task_enqueued", task=created.name, path=path)
        return created.name
