"""Thin installation-scoped GitHub REST client."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.github.auth import GitHubAuth
from app.logging_config import get_logger

log = get_logger(__name__)


class GitHubClient:
    def __init__(self, settings: Settings, auth: GitHubAuth, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._auth = auth
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        installation_id: int,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._settings.github_api_url}{path}"
        response = await self._send(method, url, installation_id, json)

        # A cached token can be revoked server-side (app suspended, permissions
        # changed). Drop it and retry exactly once.
        if response.status_code == 401:
            log.warning("github_401_retrying", path=path, installation_id=installation_id)
            self._auth.invalidate(installation_id)
            response = await self._send(method, url, installation_id, json)

        return response

    async def _send(
        self,
        method: str,
        url: str,
        installation_id: int,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        token = await self._auth.installation_token(installation_id)
        return await self._client.request(
            method,
            url,
            json=json,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
