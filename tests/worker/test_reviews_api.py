"""ReviewsAPI against a mocked GitHub: batch success, batch 422 -> individual
fallback, comment-id recovery, reply, and the two-step GraphQL resolve."""

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.github.auth import GitHubAuth
from app.github.client import GitHubClient
from tests.conftest import TEST_PRIVATE_PEM
from worker.poster.github_reviews import ReviewComment, ReviewsAPI

API = "https://api.github.com"
REPO = "acme/site"


@pytest.fixture
def api():
    settings = Settings(
        github_app_id="1", github_webhook_secret="s", github_private_key=TEST_PRIVATE_PEM, gcp_project_id="p"
    )
    http = httpx.AsyncClient()
    return ReviewsAPI(GitHubClient(settings, GitHubAuth(settings, http), http))


def _token():
    respx.post(f"{API}/app/installations/555/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs", "expires_at": "x"})
    )


@respx.mock
async def test_batch_success_recovers_comment_ids(api):
    _token()
    create = respx.post(f"{API}/repos/{REPO}/pulls/7/reviews").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )
    respx.get(f"{API}/repos/{REPO}/pulls/7/reviews/42/comments?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 501, "node_id": "N501", "path": "a.tsx", "line": 7, "body": "one"},
                {
                    "id": 502,
                    "node_id": "N502",
                    "path": "b.tsx",
                    "line": None,
                    "original_line": 9,
                    "body": "two",
                },
            ],
        )
    )
    comments = [ReviewComment("a.tsx", 7, "one", 7), ReviewComment("b.tsx", 9, "two", 8)]

    review_id, posted = await api.create_review(
        555, REPO, 7, commit_id="c" * 40, body="summary", comments=comments
    )

    assert review_id == 42
    assert [(p.id, p.path, p.line) for p in posted] == [(501, "a.tsx", 7), (502, "b.tsx", 9)]
    sent = json.loads(create.calls[0].request.content)
    assert sent["event"] == "COMMENT" and sent["commit_id"] == "c" * 40
    assert sent["comments"][0] == {"path": "a.tsx", "line": 7, "side": "RIGHT", "body": "one"}
    assert sent["comments"][1] == {
        "path": "b.tsx",
        "line": 9,
        "side": "RIGHT",
        "body": "two",
        "start_line": 8,
        "start_side": "RIGHT",
    }


@respx.mock
async def test_batch_422_falls_back_to_individual_comments(api):
    _token()
    reviews = respx.post(f"{API}/repos/{REPO}/pulls/7/reviews").mock(
        side_effect=[
            httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "pull_request_review_thread.line must be part of the diff"}],
                },
            ),
            httpx.Response(200, json={"id": 43}),  # body-only review
        ]
    )
    singles = respx.post(f"{API}/repos/{REPO}/pulls/7/comments").mock(
        side_effect=[
            httpx.Response(201, json={"id": 601, "node_id": "N601", "path": "a.tsx", "line": 7}),
            httpx.Response(422, json={"message": "Validation Failed"}),
        ]
    )
    comments = [ReviewComment("a.tsx", 7, "good"), ReviewComment("a.tsx", 99, "bad")]

    review_id, posted = await api.create_review(
        555, REPO, 7, commit_id="c" * 40, body="summary", comments=comments
    )

    assert review_id == 43
    assert reviews.call_count == 2
    assert "comments" not in json.loads(reviews.calls[1].request.content)
    assert singles.call_count == 2
    assert [(p.id, p.line) for p in posted] == [(601, 7)]
    assert json.loads(singles.calls[0].request.content)["commit_id"] == "c" * 40


@respx.mock
async def test_non_422_failure_posts_nothing(api):
    _token()
    respx.post(f"{API}/repos/{REPO}/pulls/7/reviews").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    review_id, posted = await api.create_review(
        555, REPO, 7, commit_id="c" * 40, body="s", comments=[ReviewComment("a.tsx", 1, "x")]
    )
    assert review_id is None and posted == []


@respx.mock
async def test_head_sha_and_reply(api):
    _token()
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(
        return_value=httpx.Response(200, json={"head": {"sha": "h" * 40}})
    )
    reply = respx.post(f"{API}/repos/{REPO}/pulls/7/comments/501/replies").mock(
        return_value=httpx.Response(201, json={"id": 700})
    )
    assert await api.head_sha(555, REPO, 7) == "h" * 40
    assert await api.reply(555, REPO, 7, 501, "✅ Fixed") is True
    assert json.loads(reply.calls[0].request.content) == {"body": "✅ Fixed"}


@respx.mock
async def test_resolve_thread_two_step_graphql(api):
    _token()
    gql = respx.post(f"{API}/graphql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "id": "T1",
                                            "isResolved": False,
                                            "comments": {"nodes": [{"databaseId": 400}]},
                                        },
                                        {
                                            "id": "T2",
                                            "isResolved": False,
                                            "comments": {"nodes": [{"databaseId": 501}]},
                                        },
                                    ],
                                }
                            }
                        }
                    }
                },
            ),
            httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
        ]
    )
    assert await api.resolve_thread(555, REPO, 7, 501) is True
    assert gql.call_count == 2
    mutation = json.loads(gql.calls[1].request.content)
    assert mutation["variables"] == {"id": "T2"}
    assert "resolveReviewThread" in mutation["query"]


@respx.mock
async def test_resolve_thread_already_resolved_or_missing(api):
    _token()
    gql = respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": True,
                                        "comments": {"nodes": [{"databaseId": 501}]},
                                    }
                                ],
                            }
                        }
                    }
                }
            },
        )
    )
    assert await api.resolve_thread(555, REPO, 7, 501) is True  # already resolved: nothing to do
    assert await api.resolve_thread(555, REPO, 7, 999) is False  # not ours
    assert gql.call_count == 2, "no mutation issued in either case"


@respx.mock
async def test_resolve_thread_graphql_error_is_false_not_exception(api):
    _token()
    respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "nope"}]})
    )
    assert await api.resolve_thread(555, REPO, 7, 501) is False
