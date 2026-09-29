"""Markdown for everything the bot writes on a PR.

Kept separate from the API layer so the words can be tuned without touching
retries or anchors, and so tests can pin exact output.
"""

from __future__ import annotations

from worker.poster.plan import PostedEntry, PostPlan
from worker.schema import Finding, ScanResult

BOT_NAME = "a11y-pr-bot"
_IMPACT_ORDER = ("critical", "serious", "moderate", "minor")


def _wcag(f: Finding) -> str:
    return ", ".join(c.id for c in f.wcag) or "—"


def _en(f: Finding) -> str:
    return ", ".join(f.en_301_549) or "—"


def _header(f: Finding) -> str:
    en = f" · EN 301 549 {_en(f)}" if f.en_301_549 else ""
    return f"**{f.impact.value} · {f.help}** — WCAG {_wcag(f)}{en}"


def _details(f: Finding) -> str:
    rendered = f.html.replace("`", "'")[:200]
    return (
        "<details><summary>Why this fails</summary>\n\n"
        f"{f.failure_summary}\n\n"
        f"Rendered: `{rendered}` · [rule docs]({f.help_url})\n"
        "</details>"
    )


def suggestion_comment(f: Finding) -> str:
    assert f.patch is not None
    return (
        f"{_header(f)}\n\n{f.patch.rationale}\n\n```suggestion\n{f.patch.replacement}\n```\n\n{_details(f)}"
    )


def annotation_comment(f: Finding) -> str:
    why = _why_not_one_click(f)
    body = f"{_header(f)}\n\n"
    if f.patch:
        body += f"{f.patch.rationale} {why}\n\n```tsx\n{f.patch.replacement}\n```\n\n"
    else:
        body += f"This element rendered from around here. {why}\n\n"
    return body + _details(f)


def _why_not_one_click(f: Finding) -> str:
    if f.patch and f.patch.requires_human_content:
        return "Needs a real description — edit the placeholder before committing."
    if f.source and not f.source.in_diff:
        return "This line isn't part of the diff, so it can't be a one-click suggestion."
    return f"Suggested change shown for reference ({f.disposition_reason})."


def fixed_reply(head_sha: str) -> str:
    return f"✅ Fixed in `{head_sha[:7]}` — no longer detected."


def review_body(result: ScanResult, plan: PostPlan, *, repo_full_name: str, pr_number: int) -> str:
    n_new = len(plan.new_comments) + len(plan.new_check)
    n_sugg = sum(1 for f in plan.new_comments if f.disposition == "suggestion")
    n_manual = n_new - n_sugg
    short = (result.scan_id.rsplit("_", 1)[-1])[:7]

    lines = [f"## Accessibility · {n_new} new issue{'s' if n_new != 1 else ''} on `{short}`", ""]
    parts = [f"**{n_sugg}** fixable with one click", f"**{n_manual}** need a manual edit"]
    if plan.still_open:
        parts.append(f"**{len(plan.still_open)}** still open from earlier pushes")
    if plan.fixed:
        parts.append(f"**{len(plan.fixed)}** fixed since last push ✅")
    lines += [" · ".join(parts), ""]

    ordered = sorted(plan.new_comments + plan.new_check, key=lambda f: _IMPACT_ORDER.index(f.impact.value))
    if ordered:
        lines += ["| Impact | Rule | WCAG | Where |", "|---|---|---|---|"]
        for f in ordered:
            where = f"`{f.source.file}:{f.source.line_start}`" if f.source else "—"
            lines.append(f"| {f.impact.value} | [{f.rule_id}]({f.help_url}) | {_wcag(f)} | {where} |")
        lines.append("")

    if plan.still_open:
        links = " · ".join(_comment_link(p, repo_full_name, pr_number) for _, p in plan.still_open)
        lines += [f"**Still open:** {links}", ""]

    n_review = len(result.needs_review)
    lines.append(
        f"<sub>Scanned {result.final_url or result.requested_url} with axe-core {result.engine_version}. "
        f"Automated checks cover roughly a third of WCAG 2.2"
        + (f"; {n_review} element{'s' if n_review != 1 else ''} need manual review" if n_review else "")
        + f". Details in the check run · {BOT_NAME}</sub>"
    )
    return "\n".join(lines)


def _comment_link(p: PostedEntry, repo_full_name: str, pr_number: int) -> str:
    if p.comment_id:
        return (
            f"[{p.rule_id}](https://github.com/{repo_full_name}/pull/{pr_number}#discussion_r{p.comment_id})"
        )
    return p.rule_id


def check_annotation(f: Finding) -> dict:
    assert f.source is not None
    message = f.failure_summary
    if f.patch:
        message += f"\n\nSuggested change:\n{f.patch.replacement}"
    return {
        "path": f.source.file,
        "start_line": f.source.line_start,
        "end_line": f.source.line_end,
        "annotation_level": "warning",
        "title": f"{f.rule_id}: {f.help}"[:255],
        "message": message[:64000],
    }
