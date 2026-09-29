"""Plan (pure), templates (pinned output), poster (fake GitHub + fake store)."""

from datetime import UTC, datetime

from worker.poster import templates
from worker.poster.github_reviews import PostedComment, ReviewComment
from worker.poster.plan import PostedEntry, build_plan, pr_key
from worker.poster.poster import Poster
from worker.schema import Finding, Impact, Patch, ScanResult, ScanStatus, SourceLocation, WcagCriterion

NOW = datetime.now(UTC)


def _f(
    fp,
    rule="image-alt",
    disposition="suggestion",
    file="src/A.tsx",
    line=7,
    in_diff=True,
    patch=True,
    human=False,
):
    src = SourceLocation(
        file=file, line_start=line, line_end=line, confidence=0.9, method="grep", in_diff=in_diff
    )
    p = (
        Patch(
            original=f"<img src={{x{fp}}} />",
            replacement=f'<img src={{x{fp}}} alt="Thing" />',
            rationale="Adds alt.",
            generated_by="t",
            confidence=0.9,
            requires_human_content=human,
        )
        if patch
        else None
    )
    return Finding(
        fingerprint=fp,
        rule_id=rule,
        impact=Impact.critical,
        wcag=[WcagCriterion(id="1.1.1", name="Non-text Content", level="A", introduced_in="2.0")],
        en_301_549=["9.1.1.1"],
        page_url="https://p.vercel.app/",
        page_path="/",
        selector="img",
        html=f'<img src="/x{fp}.png">',
        failure_summary="Fix any of the following:\n  Element does not have an alt attribute",
        help="Images must have alternative text",
        help_url="https://dequeuniversity.com/rules/axe/4.13/image-alt",
        source=src,
        patch=p,
        disposition=disposition,
        disposition_reason="all checks passed",
    )


def _posted(fp, comment_id=100, rule="image-alt", path="src/A.tsx", line=7, resolved=None):
    return PostedEntry(
        fingerprint=fp,
        rule_id=rule,
        kind="suggestion",
        comment_id=comment_id,
        node_id=f"n{comment_id}",
        path=path,
        line_start=line,
        resolved_at=resolved,
    )


def _result(findings):
    return ScanResult(
        scan_id="555_acme__site_" + "f3a9c2e" + "0" * 33,
        status=ScanStatus.ok,
        requested_url="https://p.vercel.app",
        final_url="https://p.vercel.app/",
        engine_version="4.13.0",
        started_at=NOW,
        finished_at=NOW,
        duration_ms=1,
        findings=findings,
        passes=3,
    )


# ---- plan ---------------------------------------------------------------


def test_plan_three_fixed_two_remaining_one_new():
    posted = [_posted(fp, comment_id=i, line=i) for i, fp in enumerate("abcde", start=1)]
    current = [_f("d", line=4), _f("e", line=5), _f("f", line=9)]
    plan = build_plan(current, posted)
    assert [f.fingerprint for f in plan.new_comments] == ["f"]
    assert sorted(p.fingerprint for _, p in plan.still_open) == ["d", "e"]
    assert sorted(p.fingerprint for p in plan.fixed) == ["a", "b", "c"]
    assert plan.new_check == []


def test_plan_same_location_new_fingerprint_is_still_open_not_new():
    """Developer touched the element (html changed -> new fingerprint) without fixing it."""
    posted = [_posted("old", comment_id=1, line=7)]
    plan = build_plan([_f("new", line=7)], posted)
    assert plan.new_comments == []
    assert [(f.fingerprint, p.fingerprint) for f, p in plan.still_open] == [("new", "old")]
    assert plan.fixed == [], "matched by location, so 'old' is not fixed"


def test_plan_out_of_diff_annotation_goes_to_check_run():
    plan = build_plan([_f("x", disposition="annotation", in_diff=False)], [])
    assert plan.new_check and plan.new_comments == []


def test_plan_ignores_drops_duplicates_and_resolved_history():
    posted = [_posted("done", resolved=NOW)]
    plan = build_plan([_f("d1", disposition="drop"), _f("d2", disposition="duplicate")], posted)
    assert not plan.new_comments and not plan.fixed and not plan.still_open


def test_plan_drop_present_this_scan_keeps_prior_open():
    """A previously posted finding that now maps poorly (drop) is still present -> not 'fixed'."""
    posted = [_posted("a", comment_id=1)]
    plan = build_plan([_f("a", disposition="drop")], posted)
    assert plan.fixed == []


def test_pr_key():
    assert pr_key(555, "acme/site.io", 7) == "555__acme__site.io__7"


# ---- templates ----------------------------------------------------------


def test_suggestion_comment_has_fence_and_details():
    body = templates.suggestion_comment(_f("a"))
    assert body.startswith(
        "**critical · Images must have alternative text** — WCAG 1.1.1 · EN 301 549 9.1.1.1"
    )
    assert '```suggestion\n<img src={xa} alt="Thing" />\n```' in body
    assert "<details><summary>Why this fails</summary>" in body
    assert "[rule docs](https://dequeuniversity.com" in body


def test_annotation_comment_never_has_suggestion_fence():
    body = templates.annotation_comment(_f("a", disposition="annotation", human=True))
    assert "```suggestion" not in body and "```tsx" in body
    assert "edit the placeholder" in body
    body = templates.annotation_comment(_f("a", disposition="annotation", in_diff=False))
    assert "isn't part of the diff" in body


def test_review_body_counts_and_links():
    posted = [_posted(fp, comment_id=i, line=i) for i, fp in enumerate("abcde", start=1)]
    current = [_f("d", line=4), _f("e", line=5), _f("f", line=9)]
    plan = build_plan(current, posted)
    body = templates.review_body(_result(current), plan, repo_full_name="acme/site", pr_number=7)
    assert body.startswith("## Accessibility · 1 new issue on `f3a9c2e`")
    assert (
        "**1** fixable with one click · **0** need a manual edit · **2** still open from earlier pushes · **3** fixed since last push ✅"
        in body
    )
    assert (
        "| critical | [image-alt](https://dequeuniversity.com/rules/axe/4.13/image-alt) | 1.1.1 | `src/A.tsx:9` |"
        in body
    )
    assert "#discussion_r4" in body and "#discussion_r5" in body
    assert body.rstrip().endswith("a11y-pr-bot</sub>")


def test_check_annotation_shape():
    a = templates.check_annotation(_f("a", disposition="annotation", in_diff=False))
    assert a["path"] == "src/A.tsx" and a["start_line"] == 7 and a["annotation_level"] == "warning"
    assert "Suggested change" in a["message"]


def test_fixed_reply():
    assert templates.fixed_reply("f3a9c2e" + "0" * 33) == "✅ Fixed in `f3a9c2e` — no longer detected."


# ---- poster against a fake GitHub ----------------------------------------


class FakeStore:
    def __init__(self, posted=None):
        self.posted = {p["fingerprint"]: p for p in (posted or [])}
        self.resolved: list[str] = []

    async def list_posted(self, key):
        return list(self.posted.values())

    async def record_posted(self, key, entries):
        for e in entries:
            self.posted[e["fingerprint"]] = {**self.posted.get(e["fingerprint"], {}), **e}

    async def mark_resolved(self, key, fps, *, head_sha):
        self.resolved += fps


class FakeReviews:
    def __init__(self, *, head=None, batch_fails=False, reject_lines=()):
        self.head = head
        self.batch_fails = batch_fails
        self.reject_lines = set(reject_lines)
        self.reviews: list[dict] = []
        self.singles: list[ReviewComment] = []
        self.replies: list[tuple[int, str]] = []
        self.resolved: list[int] = []
        self._next = 500

    async def head_sha(self, i, r, n):
        return self.head

    async def create_review(self, i, r, n, *, commit_id, body, comments):
        # Mirrors ReviewsAPI: batch, or per-comment fallback.
        if not self.batch_fails:
            self.reviews.append({"body": body, "n": len(comments)})
            posted = [self._post(c) for c in comments]
            return 42, posted
        self.reviews.append({"body": body, "n": 0})
        posted = []
        for c in comments:
            self.singles.append(c)
            if c.line not in self.reject_lines:
                posted.append(self._post(c))
        return 43, posted

    def _post(self, c):
        self._next += 1
        return PostedComment(self._next, f"node{self._next}", c.path, c.line, c.body)

    async def reply(self, i, r, n, comment_id, body):
        self.replies.append((comment_id, body))
        return True

    async def resolve_thread(self, i, r, n, comment_id):
        self.resolved.append(comment_id)
        return True


DOC = {
    "installation_id": 555,
    "repo_full_name": "acme/site",
    "pr": {"number": 7},
    "head_sha": "f3a9c2e" + "0" * 33,
}


def _hist(fp, cid, line):
    return {
        "fingerprint": fp,
        "rule_id": "image-alt",
        "kind": "suggestion",
        "comment_id": cid,
        "node_id": f"n{cid}",
        "path": "src/A.tsx",
        "line_start": line,
        "line_end": line,
        "resolved_at": None,
    }


async def test_synchronize_scenario_end_to_end():
    store = FakeStore([_hist(fp, i, i) for i, fp in enumerate("abcde", start=1)])
    gh = FakeReviews(head=DOC["head_sha"])
    current = [_f("d", line=4), _f("e", line=5), _f("f", line=9)]
    out = await Poster(store, gh).post(DOC, _result(current))

    assert out.posted == 1 and out.review_id == 42
    assert gh.reviews[0]["n"] == 1 and "3** fixed" in gh.reviews[0]["body"]
    assert sorted(cid for cid, _ in gh.replies) == [1, 2, 3]
    assert sorted(gh.resolved) == [1, 2, 3]
    assert sorted(store.resolved) == ["a", "b", "c"]
    assert store.posted["f"]["comment_id"] == 501
    f = next(x for x in current if x.fingerprint == "f")
    assert f.posted.kind == "suggestion" and f.posted.comment_id == 501
    d = next(x for x in current if x.fingerprint == "d")
    assert d.posted.comment_id == 4 and "already posted" in d.disposition_reason


async def test_batch_422_falls_back_to_individual_and_records_only_successes():
    store = FakeStore()
    gh = FakeReviews(head=DOC["head_sha"], batch_fails=True, reject_lines={9})
    current = [_f("a", line=7), _f("b", line=9)]
    out = await Poster(store, gh).post(DOC, _result(current))
    assert out.posted == 1 and out.rejected == 1 and out.review_id == 43
    assert len(gh.singles) == 2
    assert "a" in store.posted and "b" not in store.posted, "rejected anchors must not be recorded"
    b = next(x for x in current if x.fingerprint == "b")
    assert b.disposition == "drop" and "rejected" in b.disposition_reason


async def test_head_moved_skips_comments_but_still_retires_fixed():
    store = FakeStore([_hist("old", 1, 1)])
    gh = FakeReviews(head="9" * 40)
    out = await Poster(store, gh).post(DOC, _result([_f("new", line=9)]))
    assert out.skipped_head_moved is True and out.posted == 0 and gh.reviews == []
    assert gh.replies and store.resolved == ["old"]


async def test_nothing_new_posts_no_review():
    store = FakeStore([_hist("a", 1, 7)])
    gh = FakeReviews(head=DOC["head_sha"])
    out = await Poster(store, gh).post(DOC, _result([_f("a", line=7)]))
    assert gh.reviews == [] and out.posted == 0 and len(out.plan.still_open) == 1


async def test_out_of_diff_becomes_check_annotation_and_is_recorded():
    store = FakeStore()
    gh = FakeReviews(head=DOC["head_sha"])
    f = _f("x", disposition="annotation", in_diff=False)
    out = await Poster(store, gh).post(DOC, _result([f]))
    assert gh.reviews == []
    assert out.check_annotations[0]["path"] == "src/A.tsx"
    assert store.posted["x"]["kind"] == "check_annotation"
    assert f.posted.kind == "annotation" and f.posted.comment_id is None


def test_review_comment_payload_single_and_multiline():
    single = ReviewComment(path="a.tsx", line=7, start_line=7, body="b").payload()
    assert single == {"path": "a.tsx", "line": 7, "side": "RIGHT", "body": "b"}
    multi = ReviewComment(path="a.tsx", line=9, start_line=7, body="b").payload()
    assert multi["start_line"] == 7 and multi["start_side"] == "RIGHT" and multi["line"] == 9
