"""Extract a scannable preview URL from a `deployment_status` payload.

Providers set the fields differently and none of it is documented as a
contract, so this is deliberately defensive:

  Vercel    creator "vercel[bot]"; environment "Preview" | "Production";
            environment_url = the deployment URL; log_url = vercel.com inspect.
  Netlify   creator "netlify[bot]"; environment_url = deploy-preview-N--site
            .netlify.app; target_url = app.netlify.com deploy log.
  Others    Cloudflare Pages, Render, Railway etc. all set environment_url
            when they set anything. Accepted as provider "other".

Rules: only `state == "success"`; prefer environment_url, fall back to
target_url only when it is not a dashboard; skip production environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_DASHBOARD_HOSTS = ("vercel.com", "app.netlify.com", "dash.cloudflare.com", "github.com")
_PROVIDERS = {
    "vercel[bot]": "vercel",
    "netlify[bot]": "netlify",
    "cloudflare-workers-and-pages[bot]": "cloudflare",
    "cloudflare-pages[bot]": "cloudflare",
}


@dataclass(frozen=True)
class PreviewDeployment:
    url: str
    provider: str
    environment: str
    deployment_id: int | None
    sha: str


def extract_preview(payload: dict[str, Any]) -> PreviewDeployment | None:
    status = payload.get("deployment_status") or {}
    deployment = payload.get("deployment") or {}

    if status.get("state") != "success":
        return None

    sha = deployment.get("sha")
    if not sha:
        return None

    environment = str(status.get("environment") or deployment.get("environment") or "")
    if "prod" in environment.lower():
        return None

    url = _pick_url(status.get("environment_url"), status.get("target_url"))
    if url is None:
        return None

    creator = ((deployment.get("creator") or {}).get("login") or "").lower()
    provider = _PROVIDERS.get(creator, "other")

    return PreviewDeployment(
        url=url,
        provider=provider,
        environment=environment,
        deployment_id=deployment.get("id"),
        sha=sha,
    )


def _pick_url(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        host = parts.netloc.lower()
        if any(host == d or host.endswith("." + d) for d in _DASHBOARD_HOSTS):
            continue
        return candidate
    return None
