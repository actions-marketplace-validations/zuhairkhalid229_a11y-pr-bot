from datetime import UTC, datetime

from worker import report
from worker.schema import Finding, Impact, ScanResult, ScanStatus, WcagCriterion


def _result(findings=(), status=ScanStatus.ok, error=None, needs_review=()):
    now = datetime.now(UTC)
    return ScanResult(
        scan_id="s",
        status=status,
        error=error,
        requested_url="https://p.vercel.app",
        final_url="https://p.vercel.app/",
        engine_version="4.13.0",
        started_at=now,
        finished_at=now,
        duration_ms=1234,
        findings=list(findings),
        needs_review=list(needs_review),
        passes=9,
        counts_by_impact={
            i: sum(1 for f in findings if f.impact.value == i)
            for i in ("critical", "serious", "moderate", "minor")
        },
    )


def _f(rule="image-alt", impact=Impact.critical, fp="1"):
    return Finding(
        fingerprint=fp,
        rule_id=rule,
        impact=impact,
        wcag=[WcagCriterion(id="1.1.1", name="Non-text Content", level="A", introduced_in="2.0")],
        en_301_549=["9.1.1.1"],
        page_url="https://p.vercel.app/",
        page_path="/",
        selector="img",
        html="<img>",
        failure_summary="Fix: add alt",
        help="Images must have alternative text",
        help_url="https://dequeuniversity.com/rules/axe/4.13/image-alt",
    )


def test_clean_is_success_with_pass_count():
    r = _result()
    assert report.conclusion_for(r) == "success"
    assert report.title_for(r) == "No WCAG 2.2 AA issues found"
    assert "9 rules passed" in report.summary_for(r)
    assert report.text_for(r) is None


def test_findings_are_neutral_with_tables():
    r = _result([_f(fp="1"), _f(fp="2"), _f("label", Impact.serious, "3")])
    assert report.conclusion_for(r) == "neutral"
    assert report.title_for(r) == "3 accessibility issues (critical and below)"
    s = report.summary_for(r)
    assert "| critical | 2 |" in s and "| serious | 1 |" in s
    assert "[image-alt](https://dequeuniversity.com" in s and "| 1.1.1 | 9.1.1.1 | 2 |" in s
    t = report.text_for(r)
    assert t.startswith("### critical")
    assert t.count("### ") == 3


def test_failure_explains_bypass():
    r = _result(status=ScanStatus.failed_permanent, error="target returned HTTP 401")
    assert report.conclusion_for(r) == "neutral"
    assert report.title_for(r) == "Could not scan the preview"
    assert "HTTP 401" in report.summary_for(r) and "Bypass" in report.summary_for(r)


def test_needs_review_is_mentioned():
    r = _result(needs_review=[_f("color-contrast", Impact.serious)])
    assert "1 element needs manual review" in report.summary_for(r)


def test_long_lists_are_capped():
    r = _result([_f(fp=str(i)) for i in range(40)])
    assert "…and 15 more." in report.text_for(r)
