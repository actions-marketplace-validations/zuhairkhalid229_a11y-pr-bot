"""Dashboard API: token verification, the link step, and the bypass write.

firebase_admin is stubbed at the verify_id_token boundary -- the thing worth
testing is our authorisation logic, not Google's signature check.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.api.auth import Caller, current_user
from app.crypto import SecretBox
from app.main import app

API = "https://api.github.com"
KEY = SecretBox.generate_key()


class FakeStore:
    def __init__(self, members=()):
        self.members = set(members)  # (installation_id, uid)
        self.repo_config: dict[tuple[int, int], dict] = {}
        self.repos: list[dict] = []

    async def set_members(self, installation_id, uid, data):
        self.members.add((installation_id, uid))

    async def is_member(self, installation_id, uid):
        return (installation_id, uid) in self.members

    async def set_repo_config(self, installation_id, repo_id, data):
        self.repo_config[(installation_id, repo_id)] = data

    async def list_repos(self, installation_id):
        return self.repos

    async def close(self):
        pass


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.store = FakeStore()
        c.app.state.secrets = SecretBox(KEY)
        # Bypass Firebase signature verification; auth() itself is Google's job.
        c.app.dependency_overrides[current_user] = lambda: Caller(uid="uid-1", email="dev@acme.io")
        yield c
        c.app.dependency_overrides.clear()


def _installations(*entries):
    return {"total_count": len(entries), "installations": list(entries)}


def _entry(iid, app_id, login="acme", typ="Organization"):
    return {"id": iid, "app_id": app_id, "account": {"login": login, "type": typ}}


# ---- /api/link -----------------------------------------------------------


@respx.mock
def test_link_records_only_our_app_installations(client):
    respx.get(f"{API}/user/installations").mock(
        return_value=httpx.Response(
            200,
            json=_installations(
                _entry(555, 123456),  # ours
                _entry(777, 999999, login="other-app"),  # someone else's app
            ),
        )
    )
    r = client.post("/api/link", json={"github_token": "gho_usertoken"})

    assert r.status_code == 200
    assert r.json() == [{"installation_id": 555, "account_login": "acme", "account_type": "Organization"}]
    assert client.app.state.store.members == {(555, "uid-1")}


@respx.mock
def test_link_sends_the_users_token_not_an_app_jwt(client):
    route = respx.get(f"{API}/user/installations").mock(
        return_value=httpx.Response(200, json=_installations())
    )
    client.post("/api/link", json={"github_token": "gho_usertoken"})
    assert route.calls[0].request.headers["Authorization"] == "Bearer gho_usertoken"


@respx.mock
def test_link_surfaces_github_rejection(client):
    respx.get(f"{API}/user/installations").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    assert client.post("/api/link", json={"github_token": "gho_staletoken"}).status_code == 401


@respx.mock
def test_link_upstream_failure_is_502(client):
    respx.get(f"{API}/user/installations").mock(return_value=httpx.Response(500, text="boom"))
    assert client.post("/api/link", json={"github_token": "gho_upstream"}).status_code == 502


def test_link_rejects_short_token_without_calling_github(client):
    assert client.post("/api/link", json={"github_token": "x"}).status_code == 422


# ---- bypass token --------------------------------------------------------


def test_bypass_requires_membership(client):
    r = client.post("/api/installations/555/repos/99/bypass", json={"token": "vercel-secret"})
    assert r.status_code == 404, "an unlinked installation must not be confirmed to exist"
    assert client.app.state.store.repo_config == {}


def test_bypass_stores_ciphertext_never_plaintext(client):
    client.app.state.store.members.add((555, "uid-1"))
    r = client.post("/api/installations/555/repos/99/bypass", json={"token": "vercel-secret"})

    assert r.status_code == 200 and r.json() == {"has_bypass_secret": True}
    stored = client.app.state.store.repo_config[(555, 99)]["vercel_bypass_secret"]
    assert "vercel-secret" not in stored
    assert SecretBox(KEY).decrypt(stored) == "vercel-secret"


def test_bypass_empty_token_clears(client):
    client.app.state.store.members.add((555, "uid-1"))
    r = client.post("/api/installations/555/repos/99/bypass", json={"token": "   "})
    assert r.json() == {"has_bypass_secret": False}
    assert client.app.state.store.repo_config[(555, 99)]["vercel_bypass_secret"] is None


def test_bypass_without_encryption_key_refuses_rather_than_storing_plaintext(client):
    client.app.state.secrets = SecretBox("")
    client.app.state.store.members.add((555, "uid-1"))
    r = client.post("/api/installations/555/repos/99/bypass", json={"token": "vercel-secret"})
    assert r.status_code == 503
    assert client.app.state.store.repo_config == {}


# ---- repo listing --------------------------------------------------------


def test_list_repos_returns_boolean_not_ciphertext(client):
    client.app.state.store.members.add((555, "uid-1"))
    client.app.state.store.repos = [
        {"repo_id": 99, "full_name": "acme/site", "vercel_bypass_secret": "gAAAAAB-ciphertext"},
        {"repo_id": 12, "full_name": "acme/docs"},
    ]
    rows = client.get("/api/installations/555/repos").json()
    assert rows == [
        {"repo_id": 99, "full_name": "acme/site", "has_bypass_secret": True},
        {"repo_id": 12, "full_name": "acme/docs", "has_bypass_secret": False},
    ]
    assert "ciphertext" not in client.get("/api/installations/555/repos").text


def test_list_repos_requires_membership(client):
    assert client.get("/api/installations/555/repos").status_code == 404


# ---- auth ----------------------------------------------------------------


def test_missing_and_malformed_bearer_are_401():
    with TestClient(app) as c:
        c.app.state.store = FakeStore()
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}):
            r = c.get("/api/installations/555/repos", headers=headers)
            assert r.status_code == 401, headers


def test_webhook_route_is_not_behind_dashboard_auth():
    """The two auth systems must not leak into each other."""
    with TestClient(app) as c:
        c.app.state.store = FakeStore()
        # No signature -> 401 from the webhook's own HMAC check, not from Firebase.
        r = c.post("/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "ping"})
        assert r.status_code == 401
