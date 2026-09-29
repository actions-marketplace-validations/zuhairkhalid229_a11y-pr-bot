"""POST /webhooks/github -- the hot path.

Budget is one second. GitHub's delivery timeout is 10s, but a slow ACK under
load turns into redeliveries, which turn into duplicate work. Inline we do only:
verify HMAC -> claim delivery id -> hand off. Everything else is background.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status

from app.logging_config import get_logger
from app.webhooks.security import verify_signature

log = get_logger(__name__)
router = APIRouter()


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    settings = request.app.state.settings
    store = request.app.state.store
    handlers = request.app.state.handlers

    body = await request.body()

    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        # 401, not 403: the request failed to authenticate. Log the delivery id
        # only -- never the body, which is attacker-controlled at this point.
        log.warning("invalid_signature", delivery_id=x_github_delivery)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    if not x_github_event or not x_github_delivery:
        log.warning("missing_github_headers", delivery_id=x_github_delivery)
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.warning("malformed_payload", delivery_id=x_github_delivery)
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # The ping GitHub sends when you save the webhook config. Answer it before
    # the dedupe write so "Redeliver" in the UI always works while debugging.
    if x_github_event == "ping":
        log.info("ping_received", delivery_id=x_github_delivery)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    claimed = await store.claim_delivery(
        x_github_delivery, event=x_github_event, action=payload.get("action")
    )
    if not claimed:
        # Already handled. 202 anyway -- a 4xx here makes GitHub retry harder.
        return Response(status_code=status.HTTP_202_ACCEPTED)

    background_tasks.add_task(handlers.dispatch, x_github_event, payload)

    log.info(
        "webhook_accepted",
        gh_event=x_github_event,
        action=payload.get("action"),
        delivery_id=x_github_delivery,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)
