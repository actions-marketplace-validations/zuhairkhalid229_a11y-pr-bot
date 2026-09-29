"""Dashboard API. Two things the browser cannot do for itself:

  POST /api/link
      Materialise "which installations may this user see" into
      installations/{iid}/members/{uid}, which firestore.rules reads. The
      browser supplies its GitHub OAuth token; we ask GitHub, not the user.

  POST /api/installations/{iid}/repos/{rid}/bypass
      Store a Vercel Protection Bypass token. Encrypted with APP_ENCRYPTION_KEY
      from Secret Manager, which must never reach a browser -- so this cannot
      be a client-side Firestore write.

Everything else the dashboard needs it reads straight from Firestore under the
security rules.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Caller, current_user
from app.logging_config import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api")

MAX_LINKED_INSTALLATIONS = 50


class LinkRequest(BaseModel):
    # GitHub user access token from Firebase's GithubAuthProvider credential.
    github_token: str = Field(min_length=10)


class LinkedInstallation(BaseModel):
    installation_id: int
    account_login: str | None = None
    account_type: str | None = None


class BypassRequest(BaseModel):
    # Empty string clears the stored token.
    token: str = Field(default="", max_length=500)


@router.post("/link", response_model=list[LinkedInstallation])
async def link(payload: LinkRequest, request: Request, caller: Caller = Depends(current_user)):
    """Ask GitHub which installations this user can administer, then record it."""
    settings = request.app.state.settings
    store = request.app.state.store

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.github_api_url}/user/installations",
            headers={
                "Authorization": f"Bearer {payload.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"per_page": MAX_LINKED_INSTALLATIONS},
        )

    if response.status_code == 401:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "GitHub rejected that token")
    if response.status_code != 200:
        log.error("user_installations_failed", status=response.status_code, body=response.text[:300])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not reach GitHub")

    app_id = str(settings.github_app_id)
    linked: list[LinkedInstallation] = []
    for entry in response.json().get("installations", []):
        # The user may have other apps installed; only ours is ours to grant.
        if str(entry.get("app_id")) != app_id:
            continue
        account = entry.get("account") or {}
        await store.set_members(
            entry["id"],
            caller.uid,
            {"email": caller.email, "account_login": account.get("login")},
        )
        linked.append(
            LinkedInstallation(
                installation_id=entry["id"],
                account_login=account.get("login"),
                account_type=account.get("type"),
            )
        )

    log.info("user_linked", uid=caller.uid, installations=len(linked))
    return linked


@router.post("/installations/{installation_id}/repos/{repo_id}/bypass")
async def set_bypass(
    installation_id: int,
    repo_id: int,
    payload: BypassRequest,
    request: Request,
    caller: Caller = Depends(current_user),
) -> dict[str, bool]:
    store = request.app.state.store
    secrets = request.app.state.secrets

    await _require_member(store, installation_id, caller.uid)

    token = payload.token.strip()
    if not token:
        await store.set_repo_config(installation_id, repo_id, {"vercel_bypass_secret": None})
        log.info("bypass_cleared", installation_id=installation_id, repo_id=repo_id)
        return {"has_bypass_secret": False}

    if not secrets.enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "encryption key not configured")
    await store.set_repo_config(installation_id, repo_id, {"vercel_bypass_secret": secrets.encrypt(token)})
    log.info("bypass_stored", installation_id=installation_id, repo_id=repo_id)
    return {"has_bypass_secret": True}


@router.get("/installations/{installation_id}/repos")
async def list_repos(
    installation_id: int, request: Request, caller: Caller = Depends(current_user)
) -> list[dict]:
    """Repo config with the ciphertext replaced by a boolean."""
    store = request.app.state.store
    await _require_member(store, installation_id, caller.uid)
    return [
        {
            "repo_id": r.get("repo_id"),
            "full_name": r.get("full_name"),
            "has_bypass_secret": bool(r.get("vercel_bypass_secret")),
        }
        for r in await store.list_repos(installation_id)
    ]


async def _require_member(store, installation_id: int, uid: str) -> None:
    """The same check firestore.rules makes, for the write paths rules cannot
    cover. 404 rather than 403: an unlinked installation should not be
    confirmed to exist."""
    if not await store.is_member(installation_id, uid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "installation not found")
