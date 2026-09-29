"""Scanner worker. Invoked by Cloud Tasks, never by GitHub directly.

Authentication is the platform's job: deploy with --no-allow-unauthenticated
and Cloud Run verifies the OIDC token Cloud Tasks attaches (see app/queue/
tasks.py). Nothing in this process inspects it.

HTTP status is the retry signal Cloud Tasks reads:

    200  done -- scanned, permanently failed, retired, or nothing to do
    503  transient failure, attempts remaining          -> task retried
    422  payload invalid                                -> task done, logged

A permanent failure is still a 200: the PR gets a Check Run saying why, and
retrying would only burn CPU and delay that message.

`POST /tasks/scan` also works standalone with `url` and no Firestore doc --
that is the local smoke-test path and the Day 2 contract, kept intact.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from app.config import get_settings
from app.crypto import SecretBox
from app.github.auth import GitHubAuth
from app.github.checks import ChecksAPI
from app.github.client import GitHubClient
from app.logging_config import configure_logging, get_logger
from app.store.firestore import Store
from worker.config import get_worker_settings
from worker.mapper.gemini import GeminiMapper
from worker.mapper.mapper import SourceMapper
from worker.pipeline import Pipeline
from worker.poster.github_reviews import ReviewsAPI
from worker.poster.poster import Poster
from worker.scanner import Scanner
from worker.schema import ScanStatus

log = get_logger(__name__)


class ScanRequest(BaseModel):
    scan_id: str = Field(min_length=1)
    # Present when the api enqueued this; absent for a standalone/local call,
    # in which case `url` is required and nothing is persisted.
    url: HttpUrl | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)

    installation_id: int | None = None
    repo_id: int | None = None
    repo_full_name: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    check_run_id: int | None = None


class FallbackRequest(BaseModel):
    scan_id: str = Field(min_length=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    worker_settings = get_worker_settings()
    configure_logging(worker_settings.log_level)

    scanner = Scanner(worker_settings)
    await scanner.start()

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    auth = GitHubAuth(settings, http_client)
    github = GitHubClient(settings, auth, http_client)
    store = Store(settings)

    mapper = None
    if settings.gemini_api_key:
        mapper = SourceMapper(
            GeminiMapper(settings.gemini_api_key, settings.gemini_model),
            max_findings=settings.mapper_max_findings,
        )

    app.state.scanner = scanner
    app.state.store = store
    app.state.pipeline = Pipeline(
        store=store,
        checks=ChecksAPI(settings, github),
        scanner=scanner,
        secrets=SecretBox(settings.app_encryption_key),
        github=github,
        mapper=mapper,
        poster=Poster(store, ReviewsAPI(github)),
    )
    log.info(
        "worker_ready",
        axe_version=scanner.axe_version,
        mapping=mapper is not None,
        model=settings.gemini_model if mapper else None,
    )
    try:
        yield
    finally:
        await scanner.stop()
        await http_client.aclose()
        await store.close()
        log.info("worker_stopped")


app = FastAPI(title="a11y scanner worker", version="0.3.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    return {"status": "ok", "axe_version": request.app.state.scanner.axe_version}


@app.post("/tasks/scan")
async def scan(
    payload: ScanRequest,
    request: Request,
    x_cloudtasks_taskretrycount: int = Header(default=0),
) -> JSONResponse:
    pipeline: Pipeline = request.app.state.pipeline

    if payload.installation_id is None:
        # Standalone: scan the URL, return the result, persist nothing.
        if payload.url is None:
            return JSONResponse({"error": "url is required without installation_id"}, status_code=422)
        scanner: Scanner = request.app.state.scanner
        result = await scanner.scan(payload.scan_id, str(payload.url), extra_headers=payload.extra_headers)
        code = 503 if result.status is ScanStatus.failed_transient else 200
        return JSONResponse(result.model_dump(mode="json"), status_code=code)

    result, retry = await pipeline.run_scan(
        payload.scan_id,
        retry_count=x_cloudtasks_taskretrycount,
        extra_headers=payload.extra_headers,
    )
    body = result.model_dump(mode="json") if result else {"scan_id": payload.scan_id, "skipped": True}
    return JSONResponse(body, status_code=503 if retry else 200)


@app.post("/tasks/fallback")
async def fallback(payload: FallbackRequest, request: Request) -> dict[str, object]:
    pipeline: Pipeline = request.app.state.pipeline
    retired = await pipeline.run_fallback(payload.scan_id)
    return {"scan_id": payload.scan_id, "retired": retired}
