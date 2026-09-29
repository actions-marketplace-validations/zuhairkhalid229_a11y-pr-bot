"""Execute a PostPlan against GitHub and record what happened.

Order matters:
  1. Confirm the PR head is still our sha. If it moved, a newer scan owns the
     conversation; we still resolve fixed threads but post no new comments.
  2. Post the review (batch, then per-comment fallback).
  3. Record every posted fingerprint BEFORE replying/resolving anything, so a
     crash between steps cannot cause a re-post on the next run.
  4. Reply + resolve the fixed ones. Best-effort; a failure here is a
     cosmetic problem, a failure in step 3 is a duplicate comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.logging_config import get_logger
from worker.poster import templates
from worker.poster.github_reviews import PostedComment, ReviewComment, ReviewsAPI
from worker.poster.plan import PostedEntry, PostPlan, build_plan, pr_key
from worker.schema import Finding, PostedRef, ScanResult

log = get_logger(__name__)


@dataclass
class PostOutcome:
    plan: PostPlan
    review_id: int | None = None
    posted: int = 0
    rejected: int = 0
    fixed_replied: int = 0
    fixed_resolved: int = 0
    skipped_head_moved: bool = False
    check_annotations: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "posted": self.posted,
            "rejected": self.rejected,
            "still_open": len(self.plan.still_open),
            "fixed": len(self.plan.fixed),
            "fixed_resolved": self.fixed_resolved,
            "check_annotations": len(self.check_annotations),
            "skipped_head_moved": self.skipped_head_moved,
        }


class Poster:
    def __init__(self, store: Any, reviews: ReviewsAPI) -> None:
        self._store = store
        self._reviews = reviews

    async def post(self, doc: dict[str, Any], result: ScanResult) -> PostOutcome:
        installation_id = doc["installation_id"]
        repo = doc["repo_full_name"]
        pr_number = doc["pr"]["number"]
        head_sha = doc["head_sha"]
        key = pr_key(installation_id, repo, pr_number)

        history = [PostedEntry.from_doc(d) for d in await self._store.list_posted(key)]
        plan = build_plan(result.findings, history)
        outcome = PostOutcome(plan=plan)

        # Findings that resurface with a new fingerprint but the same location
        # are "still open": carry the prior comment id onto the finding.
        for f, prior in plan.still_open:
            f.posted = PostedRef(kind=prior.kind, comment_id=prior.comment_id, posted_at=_now())
            f.disposition_reason = f"already posted as {prior.fingerprint}"

        outcome.check_annotations = [templates.check_annotation(f) for f in plan.new_check]
        for f in plan.new_check:
            f.posted = PostedRef(kind="annotation", comment_id=None, posted_at=_now())

        if plan.has_review:
            current = await self._reviews.head_sha(installation_id, repo, pr_number)
            if current is not None and current != head_sha:
                log.info("post_skipped_head_moved", pr=pr_number, ours=head_sha[:7], theirs=current[:7])
                outcome.skipped_head_moved = True
            else:
                await self._post_review(outcome, key, installation_id, repo, pr_number, head_sha, result)

        # Check annotations are posted via the Check Run by the pipeline, but
        # they count as posted for dedupe purposes.
        await self._store.record_posted(
            key, [_entry(f, head_sha, "check_annotation") for f in plan.new_check]
        )

        await self._retire_fixed(outcome, key, installation_id, repo, pr_number, head_sha)
        log.info("post_complete", pr=pr_number, **outcome.summary())
        return outcome

    async def _post_review(
        self,
        outcome: PostOutcome,
        key: str,
        installation_id: int,
        repo: str,
        pr_number: int,
        head_sha: str,
        result: ScanResult,
    ) -> None:
        plan = outcome.plan
        comments: list[tuple[Finding, ReviewComment]] = []
        for f in plan.new_comments:
            assert f.source is not None
            body = (
                templates.suggestion_comment(f)
                if f.disposition == "suggestion"
                else templates.annotation_comment(f)
            )
            comments.append(
                (
                    f,
                    ReviewComment(
                        path=f.source.file,
                        line=f.source.line_end,
                        start_line=f.source.line_start,
                        body=body,
                    ),
                )
            )

        body = templates.review_body(result, plan, repo_full_name=repo, pr_number=pr_number)
        review_id, posted = await self._reviews.create_review(
            installation_id,
            repo,
            pr_number,
            commit_id=head_sha,
            body=body,
            comments=[c for _, c in comments],
        )
        outcome.review_id = review_id

        matched = _match_posted(comments, posted)
        entries = []
        for f, pc in matched:
            f.posted = PostedRef(kind=f.disposition, comment_id=pc.id if pc else None, posted_at=_now())  # type: ignore[arg-type]
            if pc is not None:
                entries.append(
                    {
                        **_entry(f, head_sha, f.disposition or "annotation"),
                        "review_id": review_id,
                        "comment_id": pc.id,
                        "node_id": pc.node_id,
                    }
                )
                outcome.posted += 1
            else:
                outcome.rejected += 1
                f.disposition, f.disposition_reason = "drop", "GitHub rejected the comment anchor"
        await self._store.record_posted(key, entries)

    async def _retire_fixed(
        self,
        outcome: PostOutcome,
        key: str,
        installation_id: int,
        repo: str,
        pr_number: int,
        head_sha: str,
    ) -> None:
        if not outcome.plan.fixed:
            return
        for p in outcome.plan.fixed:
            if p.comment_id is None:
                continue
            if await self._reviews.reply(
                installation_id, repo, pr_number, p.comment_id, templates.fixed_reply(head_sha)
            ):
                outcome.fixed_replied += 1
            if await self._reviews.resolve_thread(installation_id, repo, pr_number, p.comment_id):
                outcome.fixed_resolved += 1
        await self._store.mark_resolved(key, [p.fingerprint for p in outcome.plan.fixed], head_sha=head_sha)


def _match_posted(
    comments: list[tuple[Finding, ReviewComment]], posted: list[PostedComment]
) -> list[tuple[Finding, PostedComment | None]]:
    """GitHub returns comments without telling us which request item each
    came from; match on (path, line, body prefix)."""
    remaining = list(posted)
    out = []
    for f, rc in comments:
        hit = next(
            (p for p in remaining if p.path == rc.path and p.line == rc.line and p.body[:80] == rc.body[:80]),
            None,
        )
        if hit is None:
            hit = next((p for p in remaining if p.path == rc.path and p.body[:80] == rc.body[:80]), None)
        if hit is not None:
            remaining.remove(hit)
        out.append((f, hit))
    return out


def _entry(f: Finding, head_sha: str, kind: str) -> dict[str, Any]:
    assert f.source is not None
    return {
        "fingerprint": f.fingerprint,
        "rule_id": f.rule_id,
        "kind": kind,
        "path": f.source.file,
        "line_start": f.source.line_start,
        "line_end": f.source.line_end,
        "head_sha": head_sha,
        "comment_id": None,
        "node_id": None,
        "resolved_at": None,
    }


def _now() -> datetime:
    return datetime.now(UTC)
