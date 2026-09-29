"""The GitHub calls the poster makes. Thin, typed, and every failure mode
that matters is named here rather than discovered in production.

Review anchoring, once more because it is the whole game:
  * `line` is the LAST line of the range, `start_line` the first (omit for
    single-line). Both `side`/`start_side` are "RIGHT" -- we only ever
    comment on the new file.
  * Every line must be an added or context line of a hunk in the head
    commit's diff. Outside that: 422 for the entire review.
  * A ```suggestion fence replaces exactly the anchored lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.github.client import GitHubClient
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class ReviewComment:
    path: str
    line: int
    body: str
    start_line: int | None = None  # only for multi-line

    def payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {"path": self.path, "line": self.line, "side": "RIGHT", "body": self.body}
        if self.start_line is not None and self.start_line != self.line:
            p["start_line"] = self.start_line
            p["start_side"] = "RIGHT"
        return p


@dataclass
class PostedComment:
    id: int
    node_id: str | None
    path: str
    line: int | None
    body: str


class ReviewsAPI:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def head_sha(self, installation_id: int, repo: str, pr_number: int) -> str | None:
        r = await self._client.request("GET", f"/repos/{repo}/pulls/{pr_number}", installation_id)
        if r.status_code != 200:
            log.warning("pr_fetch_failed", status=r.status_code, pr=pr_number)
            return None
        return r.json()["head"]["sha"]

    async def create_review(
        self,
        installation_id: int,
        repo: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[ReviewComment],
    ) -> tuple[int | None, list[PostedComment]]:
        """Batch first. On 422 -- one bad anchor -- fall back to individual
        comments so the good ones still land. Returns (review_id, comments)."""
        payload = {
            "commit_id": commit_id,
            "event": "COMMENT",
            "body": body[:65000],
            "comments": [c.payload() for c in comments],
        }
        r = await self._client.request(
            "POST", f"/repos/{repo}/pulls/{pr_number}/reviews", installation_id, json=payload
        )
        if r.status_code == 200:
            review_id = r.json()["id"]
            posted = await self._review_comments(installation_id, repo, pr_number, review_id)
            log.info("review_posted", pr=pr_number, review_id=review_id, comments=len(posted))
            return review_id, posted

        log.warning(
            "review_batch_failed",
            pr=pr_number,
            status=r.status_code,
            body=r.text[:400],
            comments=len(comments),
        )
        if r.status_code != 422 or not comments:
            return None, []

        # Fallback: the summary as a bare review, then each comment alone.
        review_id = None
        r2 = await self._client.request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            installation_id,
            json={"commit_id": commit_id, "event": "COMMENT", "body": body[:65000]},
        )
        if r2.status_code == 200:
            review_id = r2.json()["id"]

        posted: list[PostedComment] = []
        for c in comments:
            one = await self._client.request(
                "POST",
                f"/repos/{repo}/pulls/{pr_number}/comments",
                installation_id,
                json={"commit_id": commit_id, **c.payload()},
            )
            if one.status_code == 201:
                j = one.json()
                posted.append(PostedComment(j["id"], j.get("node_id"), j["path"], j.get("line"), c.body))
            else:
                log.warning(
                    "comment_rejected", path=c.path, line=c.line, status=one.status_code, body=one.text[:200]
                )
        log.info(
            "review_posted_individually", pr=pr_number, ok=len(posted), rejected=len(comments) - len(posted)
        )
        return review_id, posted

    async def _review_comments(
        self, installation_id: int, repo: str, pr_number: int, review_id: int
    ) -> list[PostedComment]:
        r = await self._client.request(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments?per_page=100",
            installation_id,
        )
        if r.status_code != 200:
            log.warning("review_comments_fetch_failed", status=r.status_code)
            return []
        return [
            PostedComment(
                j["id"], j.get("node_id"), j["path"], j.get("line") or j.get("original_line"), j["body"]
            )
            for j in r.json()
        ]

    async def reply(
        self, installation_id: int, repo: str, pr_number: int, comment_id: int, body: str
    ) -> bool:
        r = await self._client.request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            installation_id,
            json={"body": body},
        )
        if r.status_code != 201:
            log.info("reply_failed", comment_id=comment_id, status=r.status_code)
        return r.status_code == 201

    async def resolve_thread(self, installation_id: int, repo: str, pr_number: int, comment_id: int) -> bool:
        """GraphQL only: find the thread whose first comment is ours, resolve it."""
        owner, name = repo.split("/", 1)
        query = """
        query($owner:String!,$name:String!,$number:Int!,$after:String){
          repository(owner:$owner,name:$name){ pullRequest(number:$number){
            reviewThreads(first:100, after:$after){
              pageInfo{ hasNextPage endCursor }
              nodes{ id isResolved comments(first:1){ nodes{ databaseId } } } } } } }"""
        after = None
        thread_id = None
        for _ in range(5):  # 500 threads is plenty
            r = await self._client.request(
                "POST",
                "/graphql",
                installation_id,
                json={
                    "query": query,
                    "variables": {"owner": owner, "name": name, "number": pr_number, "after": after},
                },
            )
            if r.status_code != 200 or "errors" in r.json():
                log.info("graphql_threads_failed", status=r.status_code, body=r.text[:200])
                return False
            threads = r.json()["data"]["repository"]["pullRequest"]["reviewThreads"]
            for t in threads["nodes"]:
                first = (t["comments"]["nodes"] or [{}])[0]
                if first.get("databaseId") == comment_id:
                    if t["isResolved"]:
                        return True
                    thread_id = t["id"]
                    break
            if thread_id or not threads["pageInfo"]["hasNextPage"]:
                break
            after = threads["pageInfo"]["endCursor"]
        if thread_id is None:
            return False

        r = await self._client.request(
            "POST",
            "/graphql",
            installation_id,
            json={
                "query": "mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }",
                "variables": {"id": thread_id},
            },
        )
        ok = r.status_code == 200 and "errors" not in r.json()
        if not ok:
            log.info("resolve_thread_failed", status=r.status_code, body=r.text[:200])
        return ok
