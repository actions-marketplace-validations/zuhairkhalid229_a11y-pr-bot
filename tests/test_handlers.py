"""The background chain: JWT -> installation token -> Check Run -> store -> queue.

GitHub is mocked at the HTTP layer with respx so the real auth and checks code
runs. The store and queue are faked -- their contracts are one method each.
"""

import httpx
import pytest
import respx

from app.config import Settings
from app.github.auth import GitHubAuth
from app.github.checks import ChecksAPI
from app.github.client import GitHubClient
from app.webhooks.handlers import EventHandlers
from tests.conftest import TEST_PRIVATE_PEM

API = "https://api.github.com"


from app.store.scan_state import apply_half


class FakeStore:
    """Same contract as Store, backed by dicts, using the real merge function."""

    def __init__(self):
        self.scans: dict[str, dict] = {}
        self.installations: dict[int, dict] = {}

    async def register_half(self, scan_id, *, base, pr=None, preview=None):
        merged = apply_half(self.scans.get(scan_id), pr=pr, preview=preview)
        self.scans[scan_id] = {**base, **self.scans.get(scan_id, {}), **merged.doc}
        return merged

    async def update_scan(self, scan_id, data):
        self.scans.setdefault(scan_id, {}).update(data)

    async def get_scan(self, scan_id):
        return self.scans.get(scan_id)

    async def record_installation(self, installation_id, data):
        self.installations[installation_id] = data


class FakeQueue:
    def __init__(self):
        self.enqueued: list[dict] = []
        self.fallbacks: list[str] = []

    async def enqueue_scan(self, payload):
        self.enqueued.append(payload)
        return "task-name"

    async def enqueue_fallback(self, scan_id):
        self.fallbacks.append(scan_id)
        return f"{scan_id}-fallback"


@pytest.fixture
def settings():
    return Settings(
        github_app_id="123456",
        github_webhook_secret="s",
        github_private_key=TEST_PRIVATE_PEM,
        gcp_project_id="p",
    )


@pytest.fixture
def wiring(settings):
    http = httpx.AsyncClient()
    auth = GitHubAuth(settings, http)
    github = GitHubClient(settings, auth, http)
    store, queue = FakeStore(), FakeQueue()
    handlers = EventHandlers(settings=settings, store=store, checks=ChecksAPI(settings, github), queue=queue)
    return handlers, store, queue, auth


PR_PAYLOAD = {
    "action": "opened",
    "pull_request": {
        "number": 7,
        "draft": False,
        "head": {"sha": "a" * 40, "ref": "feat/x"},
        "base": {"ref": "main"},
    },
    "repository": {"full_name": "acme/site", "id": 99},
    "installation": {"id": 555},
}


@respx.mock
async def test_pull_request_opened_creates_queued_check_run(wiring):
    handlers, store, queue, _ = wiring

    token_route = respx.post(f"{API}/app/installations/555/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc", "expires_at": "x"})
    )
    check_route = respx.post(f"{API}/repos/acme/site/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 4242})
    )

    await handlers.dispatch("pull_request", PR_PAYLOAD)

    # Auth: JWT went to the token endpoint, token went to the check-run call.
    assert token_route.called
    assert token_route.calls[0].request.headers["Authorization"].startswith("Bearer ey")
    assert check_route.calls[0].request.headers["Authorization"] == "Bearer ghs_abc"

    # Check Run payload is what GitHub expects.
    import json

    sent = json.loads(check_route.calls[0].request.content)
    assert sent["status"] == "queued"
    assert sent["head_sha"] == "a" * 40
    assert sent["name"] == "Accessibility (WCAG 2.2 AA)"

    # PR half only: waiting for a preview, fallback armed, nothing enqueued yet.
    scan_id = "555_acme__site_" + "a" * 40
    assert store.scans[scan_id]["check_run_id"] == 4242
    assert store.scans[scan_id]["state"] == "awaiting_deployment"
    assert store.scans[scan_id]["pr"]["number"] == 7
    assert queue.enqueued == []
    assert queue.fallbacks == [scan_id]


@respx.mock
async def test_installation_token_is_cached_across_calls(wiring):
    handlers, _, _, _auth = wiring

    token_route = respx.post(f"{API}/app/installations/555/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc", "expires_at": "x"})
    )
    respx.post(f"{API}/repos/acme/site/check-runs").mock(return_value=httpx.Response(201, json={"id": 1}))

    await handlers.dispatch("pull_request", PR_PAYLOAD)
    await handlers.dispatch("pull_request", {**PR_PAYLOAD, "action": "synchronize"})

    assert token_route.call_count == 1, "second PR event must reuse the cached token"


@respx.mock
async def test_401_invalidates_token_and_retries_once(wiring):
    handlers, store, _, _ = wiring

    token_route = respx.post(f"{API}/app/installations/555/access_tokens").mock(
        side_effect=[
            httpx.Response(201, json={"token": "ghs_old", "expires_at": "x"}),
            httpx.Response(201, json={"token": "ghs_new", "expires_at": "x"}),
        ]
    )
    check_route = respx.post(f"{API}/repos/acme/site/check-runs").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Bad credentials"}),
            httpx.Response(201, json={"id": 77}),
        ]
    )

    await handlers.dispatch("pull_request", PR_PAYLOAD)

    assert token_route.call_count == 2
    assert check_route.call_count == 2
    assert check_route.calls[1].request.headers["Authorization"] == "Bearer ghs_new"
    assert next(iter(store.scans.values()))["check_run_id"] == 77


async def test_draft_and_irrelevant_actions_are_ignored(wiring):
    handlers, store, queue, _ = wiring

    await handlers.dispatch("pull_request", {**PR_PAYLOAD, "action": "labeled"})
    draft = {**PR_PAYLOAD, "pull_request": {**PR_PAYLOAD["pull_request"], "draft": True}}
    await handlers.dispatch("pull_request", draft)

    assert store.scans == {}
    assert queue.enqueued == []


async def test_installation_event_recorded(wiring):
    handlers, store, _, _ = wiring

    await handlers.dispatch(
        "installation",
        {
            "action": "created",
            "installation": {
                "id": 555,
                "app_id": 123456,
                "account": {"login": "acme", "type": "Organization"},
                "repository_selection": "selected",
                "suspended_at": None,
            },
        },
    )

    assert store.installations[555]["account_login"] == "acme"
    assert store.installations[555]["plan"] == "free"
    assert store.installations[555]["suspended"] is False


async def test_handler_exception_does_not_propagate(wiring):
    """The 202 is already sent; a crash here must be logged, not raised."""
    handlers, _, _, _ = wiring
    await handlers.dispatch("pull_request", {"action": "opened"})  # missing keys


# ---- Day 3: the two halves in both orders --------------------------------


def _deployment_payload(url="https://site-git-feat-acme.vercel.app", state="success", sha="a" * 40):
    return {
        "action": "created",
        "deployment_status": {
            "state": state,
            "environment": "Preview",
            "environment_url": url,
            "target_url": url,
            "log_url": "https://vercel.com/acme/site/x",
        },
        "deployment": {
            "id": 991,
            "sha": sha,
            "environment": "Preview",
            "creator": {"login": "vercel[bot]", "type": "Bot"},
        },
        "repository": {"id": 99, "full_name": "acme/site"},
        "installation": {"id": 555},
    }


def _mock_github(check_id=4242):
    respx.post(f"{API}/app/installations/555/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc", "expires_at": "x"})
    )
    return respx.post(f"{API}/repos/acme/site/check-runs").mock(
        return_value=httpx.Response(201, json={"id": check_id})
    )


@respx.mock
async def test_pr_then_deployment_enqueues_exactly_once(wiring):
    handlers, store, queue, _ = wiring
    _mock_github()
    scan_id = "555_acme__site_" + "a" * 40

    await handlers.dispatch("pull_request", PR_PAYLOAD)
    assert store.scans[scan_id]["state"] == "awaiting_deployment"
    assert queue.enqueued == [] and queue.fallbacks == [scan_id]

    await handlers.dispatch("deployment_status", _deployment_payload())
    assert store.scans[scan_id]["state"] == "queued"
    assert len(queue.enqueued) == 1
    task = queue.enqueued[0]
    assert task["url"] == "https://site-git-feat-acme.vercel.app"
    assert task["pr_number"] == 7
    assert task["check_run_id"] == 4242
    assert task["repo_id"] == 99
    assert "vercel_bypass" not in str(task), "secrets never ride in the task payload"

    # Redelivery / redeploy of the same sha: nothing more happens.
    await handlers.dispatch("deployment_status", _deployment_payload(url="https://redeploy.vercel.app"))
    await handlers.dispatch("pull_request", {**PR_PAYLOAD, "action": "synchronize"})
    assert len(queue.enqueued) == 1
    assert store.scans[scan_id]["preview"]["url"] == "https://site-git-feat-acme.vercel.app"


@respx.mock
async def test_deployment_then_pr_enqueues_exactly_once_without_fallback(wiring):
    handlers, store, queue, _ = wiring
    check_route = _mock_github()
    scan_id = "555_acme__site_" + "a" * 40

    await handlers.dispatch("deployment_status", _deployment_payload())
    assert store.scans[scan_id]["state"] == "awaiting_pr"
    assert not check_route.called, "no Check Run until we know this sha is a PR"
    assert queue.enqueued == []

    await handlers.dispatch("pull_request", PR_PAYLOAD)
    assert store.scans[scan_id]["state"] == "queued"
    assert check_route.called
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["check_run_id"] == 4242
    assert queue.fallbacks == [], "preview already known; no fallback needed"


async def test_non_success_and_production_deployments_are_ignored(wiring):
    handlers, store, queue, _ = wiring
    await handlers.dispatch("deployment_status", _deployment_payload(state="pending"))
    prod = _deployment_payload()
    prod["deployment_status"]["environment"] = "Production"
    await handlers.dispatch("deployment_status", prod)
    assert store.scans == {} and queue.enqueued == []


async def test_deployment_for_unrelated_sha_does_not_touch_pr_scan(wiring):
    handlers, store, queue, _ = wiring
    await handlers.dispatch("deployment_status", _deployment_payload(sha="c" * 40))
    assert "555_acme__site_" + "c" * 40 in store.scans
    assert store.scans["555_acme__site_" + "c" * 40]["state"] == "awaiting_pr"
    assert queue.enqueued == []
