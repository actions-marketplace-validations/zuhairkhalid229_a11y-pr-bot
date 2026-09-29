"""GitHub App authentication: RS256 app JWT -> cached installation token.

Two credentials are in play and they are easy to confuse:

  * The **app JWT** is signed with the app's private key. It identifies the app
    itself and may only call `/app/*` endpoints. Max lifetime 10 minutes.
  * The **installation access token** is minted with that JWT for one specific
    installation. It is what you use for every repo-scoped call. Lives 1 hour.

Tokens are cached in-process only. A second Cloud Run instance simply mints its
own -- deliberate, so short-lived credentials are never written to Firestore.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx
import jwt

from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Refresh this many seconds before real expiry, so an in-flight request never
# races the boundary.
_EXPIRY_SKEW_SECONDS = 300
# GitHub caps app JWTs at 10 minutes; stay under it to tolerate clock drift.
_JWT_LIFETIME_SECONDS = 540


@dataclass(frozen=True)
class InstallationToken:
    value: str
    expires_at: float

    @property
    def is_fresh(self) -> bool:
        return time.time() < self.expires_at - _EXPIRY_SKEW_SECONDS


class GitHubAuthError(RuntimeError):
    pass


class GitHubAuth:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._cache: dict[int, InstallationToken] = {}
        # One lock per installation: concurrent webhooks for the same repo must
        # not all stampede the token endpoint.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def app_jwt(self) -> str:
        """Sign a short-lived JWT proving we hold the app's private key."""
        now = int(time.time())
        payload = {
            # Backdate to absorb clock skew between us and GitHub.
            "iat": now - 60,
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": self._settings.github_app_id,
        }
        return jwt.encode(payload, self._settings.private_key_pem, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and cached.is_fresh:
            return cached.value

        async with self._locks[installation_id]:
            # Re-check: another coroutine may have refreshed while we waited.
            cached = self._cache.get(installation_id)
            if cached and cached.is_fresh:
                return cached.value

            token = await self._mint(installation_id)
            self._cache[installation_id] = token
            return token.value

    async def _mint(self, installation_id: int) -> InstallationToken:
        url = f"{self._settings.github_api_url}/app/installations/{installation_id}/access_tokens"
        response = await self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 201:
            log.error(
                "installation_token_failed",
                installation_id=installation_id,
                status=response.status_code,
                body=response.text[:500],
            )
            raise GitHubAuthError(f"could not mint installation token ({response.status_code})")

        payload = response.json()
        # GitHub returns ISO-8601; trusting our own clock + 1h is enough given
        # the 5 minute refresh skew above.
        log.info("installation_token_minted", installation_id=installation_id)
        return InstallationToken(value=payload["token"], expires_at=time.time() + 3600)

    def invalidate(self, installation_id: int) -> None:
        """Drop a cached token, e.g. after a 401 from the API."""
        self._cache.pop(installation_id, None)
