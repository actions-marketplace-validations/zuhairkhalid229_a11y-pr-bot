"""What to post, as a pure function of this scan's findings and the PR's
posting history. No I/O here; poster.py executes the plan.

    new        postable findings never posted on this PR   -> review comment
    still_open postable findings already posted            -> listed, not re-posted
    fixed      posted findings absent from this scan       -> reply + resolve
    check      mapped but outside the diff                 -> Check Run annotation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from worker.schema import Finding

POSTABLE = ("suggestion", "annotation")


@dataclass
class PostedEntry:
    fingerprint: str
    rule_id: str
    kind: str
    comment_id: int | None
    node_id: str | None
    path: str | None
    line_start: int | None
    resolved_at: Any = None

    @classmethod
    def from_doc(cls, d: dict[str, Any]) -> PostedEntry:
        return cls(
            fingerprint=d["fingerprint"],
            rule_id=d.get("rule_id", ""),
            kind=d.get("kind", ""),
            comment_id=d.get("comment_id"),
            node_id=d.get("node_id"),
            path=d.get("path"),
            line_start=d.get("line_start"),
            resolved_at=d.get("resolved_at"),
        )


@dataclass
class PostPlan:
    new_comments: list[Finding] = field(default_factory=list)  # review comments
    new_check: list[Finding] = field(default_factory=list)  # check-run annotations
    still_open: list[tuple[Finding, PostedEntry]] = field(default_factory=list)
    fixed: list[PostedEntry] = field(default_factory=list)

    @property
    def has_review(self) -> bool:
        return bool(self.new_comments)


def build_plan(findings: list[Finding], posted: list[PostedEntry]) -> PostPlan:
    plan = PostPlan()
    by_fp = {p.fingerprint: p for p in posted}
    by_loc = {
        (p.rule_id, p.path, p.line_start): p
        for p in posted
        if p.path and p.line_start and p.resolved_at is None
    }
    present: set[str] = {f.fingerprint for f in findings}

    for f in findings:
        if f.disposition not in POSTABLE or f.source is None:
            continue
        prior = by_fp.get(f.fingerprint) or by_loc.get((f.rule_id, f.source.file, f.source.line_start))
        if prior is not None:
            present.add(prior.fingerprint)
            if prior.resolved_at is None:
                plan.still_open.append((f, prior))
            continue
        if f.disposition == "suggestion" or f.source.in_diff:
            plan.new_comments.append(f)
        else:
            plan.new_check.append(f)

    for p in posted:
        if p.resolved_at is None and p.fingerprint not in present:
            plan.fixed.append(p)

    return plan


def pr_key(installation_id: int, repo_full_name: str, pr_number: int) -> str:
    return f"{installation_id}__{repo_full_name.replace('/', '__')}__{pr_number}"
