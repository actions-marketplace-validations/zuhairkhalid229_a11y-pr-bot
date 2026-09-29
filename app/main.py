"""FastAPI entrypoint.

Cloud Run deployment note -- this one matters:

    gcloud run deploy a11y-api --no-cpu-throttling ...

By default Cloud Run throttles CPU to near zero once a response is sent. This
service returns 202 and *then* does its real work in a BackgroundTask, so with
throttling on, Check Run creation stalls until the next request happens to wake
the instance. Either deploy with --no-cpu-throttling, or move the Check Run
creation into the Cloud Tasks worker.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router as api_router
from app.config import get_settings
from app.crypto import SecretBox
from app.github.auth import GitHubAuth
from app.github.checks import ChecksAPI
from app.github.client import GitHubClient
from app.logging_config import configure_logging, get_logger
from app.queue.tasks import TaskQueue
from app.store.firestore import Store
from app.webhooks.handlers import EventHandlers
from app.webhooks.router import router as webhook_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    auth = GitHubAuth(settings, http_client)
    github = GitHubClient(settings, auth, http_client)
    store = Store(settings)

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.store = store
    app.state.secrets = SecretBox(settings.app_encryption_key)
    app.state.handlers = EventHandlers(
        settings=settings,
        store=store,
        checks=ChecksAPI(settings, github),
        queue=TaskQueue(settings),
    )

    log.info("startup_complete", app_id=settings.github_app_id)
    try:
        yield
    finally:
        await http_client.aclose()
        await store.close()
        log.info("shutdown_complete")


app = FastAPI(title="a11y PR bot", version="0.6.0", lifespan=lifespan)

# The dashboard is a different origin (Vercel) to this service (Cloud Run), so
# the /api routes need CORS. The webhook does not -- GitHub is not a browser.
# Read straight from the environment: middleware is fixed at app construction,
# before the lifespan runs, and get_settings() would demand the full config
# just to import this module.
_origins = [
    o.strip() for o in os.environ.get("DASHBOARD_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:3000"],
    allow_credentials=False,  # bearer token, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(webhook_router)
app.include_router(api_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
