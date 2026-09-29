"""Firebase ID token verification for the dashboard endpoints.

firebase_admin.auth.verify_id_token checks signature, expiry, audience and
issuer against Google's rotating public keys. We run it in a thread because the
Admin SDK is synchronous and key fetches occasionally hit the network.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _init_admin(project_id: str):
    import firebase_admin
    from firebase_admin import credentials

    try:
        return firebase_admin.get_app()
    except ValueError:
        # On Cloud Run the default service account is the credential; no key file.
        return firebase_admin.initialize_app(credentials.ApplicationDefault(), {"projectId": project_id})


class Caller:
    def __init__(self, uid: str, email: str | None) -> None:
        self.uid = uid
        self.email = email


async def current_user(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Caller:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1]

    from firebase_admin import auth as fb_auth

    _init_admin(settings.gcp_project_id)
    try:
        claims = await asyncio.to_thread(fb_auth.verify_id_token, token)
    except Exception as exc:  # InvalidIdTokenError, ExpiredIdTokenError, ...
        log.info("id_token_rejected", error=type(exc).__name__)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc

    return Caller(uid=claims["uid"], email=claims.get("email"))
