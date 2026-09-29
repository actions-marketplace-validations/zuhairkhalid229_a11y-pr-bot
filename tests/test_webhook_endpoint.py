"""End-to-end checks on the hot path, with the store and handlers faked."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.webhooks.security import compute_signature

SECRET = "test-webhook-secret"


class FakeStore:
    def __init__(self):
        self.claimed: list[str] = []

    async def claim_delivery(self, delivery_id, *, event, action):
        if delivery_id in self.claimed:
            return False
        self.claimed.append(delivery_id)
        return True

    async def close(self):
        pass


class FakeHandlers:
    def __init__(self):
        self.dispatched: list[tuple[str, dict]] = []

    async def dispatch(self, event, payload):
        self.dispatched.append((event, payload))


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.store = FakeStore()
        c.app.state.handlers = FakeHandlers()
        yield c


def post(client, payload: dict, *, event="pull_request", delivery="d-1", secret=SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": compute_signature(secret, body),
            "Content-Type": "application/json",
        },
    )


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


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_valid_webhook_returns_202_and_dispatches(client):
    response = post(client, PR_PAYLOAD)
    assert response.status_code == 202
    assert client.app.state.handlers.dispatched == [("pull_request", PR_PAYLOAD)]


def test_ack_is_under_one_second(client):
    start = time.perf_counter()
    response = post(client, PR_PAYLOAD, delivery="d-timing")
    elapsed = time.perf_counter() - start
    assert response.status_code == 202
    assert elapsed < 1.0, f"ACK took {elapsed:.3f}s, budget is 1s"


def test_bad_signature_rejected_without_dispatch(client):
    body = json.dumps(PR_PAYLOAD).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-bad",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert response.status_code == 401
    assert client.app.state.handlers.dispatched == []


def test_missing_signature_rejected(client):
    response = client.post(
        "/webhooks/github",
        content=b"{}",
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-nosig"},
    )
    assert response.status_code == 401


def test_ping_answered_before_dedupe(client):
    response = post(client, {"zen": "hi"}, event="ping", delivery="d-ping")
    assert response.status_code == 204
    assert client.app.state.store.claimed == []


def test_replayed_delivery_is_dropped(client):
    assert post(client, PR_PAYLOAD, delivery="d-dupe").status_code == 202
    assert post(client, PR_PAYLOAD, delivery="d-dupe").status_code == 202
    # Second delivery must not reach the handler.
    assert len(client.app.state.handlers.dispatched) == 1


def test_malformed_json_rejected(client):
    body = b"{not json"
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-bad-json",
            "X-Hub-Signature-256": compute_signature(SECRET, body),
        },
    )
    assert response.status_code == 400
