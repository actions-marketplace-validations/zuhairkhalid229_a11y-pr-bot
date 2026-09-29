"""Check Run output for a finished scan.

GitHub renders `summary` and `text` as Markdown on the Checks tab. Limits are
65535 chars each; we cap far below because a wall of findings on a Check Run
is not something anyone reads -- the per-line review comments (Day 5) are the
real UI, this is the overview.
"""

from __future__ import annotations

from collections import Counter

from worker.schema import Finding, ScanResult, ScanStatus

_IMPACT_ORDER = ("critical", "serious", "moderate", "minor")
_MAX_LISTED = 25


def conclusion_for(result: ScanResult) -> str:
    """`success` only when clean. Findings are `neutral`, not `failure`: a free
    tool that blocks merges by default gets uninstalled. Day 6 makes the
    threshold a per-repo setting."""
    if result.status is ScanStatus.ok and not result.findings:
        return "success"
    return "neutral"


def title_for(result: ScanResult) -> str:
    if result.status is not ScanStatus.ok:
        return "Could not scan the preview"
    n = len(result.findings)
    if n == 0:
        return "No WCAG 2.2 AA issues found"
    counts = result.counts_by_impact
    worst = next((i for i in _IMPACT_ORDER if counts.get(i)), "moderate")
    return f"{n} accessibility issue{'s' if n != 1 else ''} ({worst} and below)"


def summary_for(result: ScanResult, *, posting: dict | None = None) -> str:
    if result.status is not ScanStatus.ok:
        return (
            f"**{result.error}**\n\n"
            f"URL: `{result.requested_url}`\n\n"
            "If this preview is protected by Vercel Authentication, add the "
            "project's *Protection Bypass for Automation* token in the app "
            "settings so the scanner can reach it."
        )

    lines = [
        f"Scanned `{result.final_url or result.requested_url}` "
        f"with axe-core {result.engine_version} at {result.viewport} "
        f"in {result.duration_ms / 1000:.1f}s."
    ]

    if not result.findings:
        lines.append(f"\n{result.passes} rules passed. Nothing to fix on this page.")
        if result.needs_review:
            lines.append(_needs_review_line(result))
        if posting:
            lines.append(_posting_line(posting))
        return "\n".join(lines)

    counts = result.counts_by_impact
    lines.append("")
    lines.append("| Impact | Count |")
    lines.append("|---|---|")
    for impact in _IMPACT_ORDER:
        if counts.get(impact):
            lines.append(f"| {impact} | {counts[impact]} |")

    by_rule = Counter(f.rule_id for f in result.findings)
    lines.append("")
    lines.append("| Rule | WCAG | EN 301 549 | Elements |")
    lines.append("|---|---|---|---|")
    for rule_id, count in by_rule.most_common():
        sample = next(f for f in result.findings if f.rule_id == rule_id)
        wcag = ", ".join(c.id for c in sample.wcag) or "—"
        en = ", ".join(sample.en_301_549) or "—"
        lines.append(f"| [{rule_id}]({sample.help_url}) | {wcag} | {en} | {count} |")

    if result.needs_review:
        lines.append(_needs_review_line(result))
    if posting:
        lines.append(_posting_line(posting))
    return "\n".join(lines)


def _posting_line(p: dict) -> str:
    bits = []
    if p.get("posted"):
        bits.append(f"{p['posted']} review comment{'s' if p['posted'] != 1 else ''} posted")
    if p.get("still_open"):
        bits.append(f"{p['still_open']} still open from earlier pushes")
    if p.get("fixed"):
        bits.append(f"{p['fixed']} fixed since last push")
    if p.get("skipped_head_moved"):
        bits.append("comments skipped: a newer commit was pushed")
    return ("\n" + " · ".join(bits)) if bits else ""


def text_for(result: ScanResult) -> str | None:
    """Per-element detail, capped. Sorted worst first."""
    if result.status is not ScanStatus.ok or not result.findings:
        return None

    ordered = sorted(result.findings, key=lambda f: _IMPACT_ORDER.index(f.impact.value))
    parts = []
    for f in ordered[:_MAX_LISTED]:
        parts.append(_finding_block(f))
    if len(ordered) > _MAX_LISTED:
        parts.append(f"\n_…and {len(ordered) - _MAX_LISTED} more._")
    return "\n".join(parts)


def _finding_block(f: Finding) -> str:
    wcag = ", ".join(f"{c.id} {c.name}" for c in f.wcag) or "—"
    return (
        f"### {f.impact.value} · {f.help}\n"
        f"**WCAG:** {wcag}  \n"
        f"**Selector:** `{f.selector}`\n"
        f"```html\n{f.html[:300]}\n```\n"
        f"{f.failure_summary}\n"
    )


def _needs_review_line(result: ScanResult) -> str:
    n = len(result.needs_review)
    return (
        f"\n{n} element{'s' if n != 1 else ''} need{'s' if n == 1 else ''} "
        "manual review (axe could not decide automatically)."
    )
