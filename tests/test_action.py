"""The action's reporting and gating logic.

The scan itself is already covered by tests/worker/. What is untested elsewhere
is the layer a consumer's workflow actually depends on: the exit code, the
outputs, and whether the report tells the truth.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worker.schema import Finding, Impact, ScanResult, ScanStatus, WcagCriterion

spec = importlib.util.spec_from_file_location(
    "action_scan", Path(__file__).resolve().parent.parent / "action" / "scan.py"
)
action = importlib.util.module_from_spec(spec)
sys.modules["action_scan"] = action
spec.loader.exec_module(action)

NOW = datetime.now(UTC)


def _finding(impact=Impact.critical, rule="image-alt", fp="a"):
    return Finding(
        fingerprint=fp,
        rule_id=rule,
        impact=impact,
        wcag=[WcagCriterion(id="1.1.1", name="Non-text Content", level="A", introduced_in="2.0")],
        en_301_549=["9.1.1.1"],
        page_url="http://localhost:8000/x.html",
        page_path="/x.html",
        selector="img",
        html='<img src="/logo.svg">',
        failure_summary="Element does not have an alt attribute",
        help="Images must have alternative text",
        help_url="https://dequeuniversity.com/rules/axe/4.13/image-alt",
    )


def _result(findings=(), status=ScanStatus.ok, error=None, needs_review=()):
    return ScanResult(
        scan_id="action",
        status=status,
        error=error,
        requested_url="http://localhost:8000/x.html",
        final_url="http://localhost:8000/x.html",
        engine_version="4.13.0",
        started_at=NOW,
        finished_at=NOW,
        duration_ms=900,
        findings=list(findings),
        needs_review=list(needs_review),
        passes=12,
    )


# ---- the gate ------------------------------------------------------------


@pytest.mark.parametrize(
    "impact,fail_on,expected",
    [
        (Impact.critical, "critical", True),
        (Impact.critical, "serious", True),
        (Impact.critical, "minor", True),
        (Impact.serious, "critical", False),
        (Impact.moderate, "serious", False),
        (Impact.minor, "minor", True),
        (Impact.critical, "never", False),
        (Impact.minor, "never", False),
    ],
)
def test_fail_on_is_impact_or_higher(impact, fail_on, expected):
    assert action.should_fail(_result([_finding(impact=impact)]), fail_on) is expected


def test_clean_scan_never_fails():
    for fail_on in ("critical", "serious", "moderate", "minor", "never"):
        assert action.should_fail(_result(), fail_on) is False


def test_unknown_fail_on_does_not_fail_the_build():
    """A typo in a workflow must not silently start blocking merges."""
    assert action.should_fail(_result([_finding()]), "CRITICAL!!") is False


def test_worst_impact_ordering():
    result = _result([_finding(Impact.minor, fp="1"), _finding(Impact.serious, fp="2")])
    assert action.worst_impact(result) == "serious"
    assert action.worst_impact(_result()) is None


# ---- the report ----------------------------------------------------------


def test_report_on_violations_is_honest_and_complete():
    result = _result(
        [_finding(fp="1"), _finding(fp="2"), _finding(Impact.serious, "label", "3")],
        needs_review=[_finding(Impact.moderate, "color-contrast", "4")],
    )
    md = action.render_report(result, "http://x/y.html", "serious")

    assert md.startswith("## Accessibility: 3 violations")
    assert "| critical | 2 |" in md and "| serious | 1 |" in md
    assert "| [image-alt](https://dequeuniversity.com" in md
    assert "| 1.1.1 | 9.1.1.1 | 2 |" in md, "WCAG and EN 301 549 columns"
    assert "1 element could not be decided automatically" in md
    assert "Failing the job on **serious**" in md
    assert "roughly a third of WCAG 2.2" in md, "coverage caveat must survive edits"


def test_report_when_clean():
    md = action.render_report(_result(), "http://x/y.html", "never")
    assert "no WCAG 2.2 AA violations found" in md
    assert "12 axe rules passed" in md


def test_report_on_failure_points_at_deployment_protection():
    result = _result(status=ScanStatus.failed_permanent, error="target returned HTTP 401")
    md = action.render_report(result, "http://x/y.html", "never")
    assert "could not run" in md and "HTTP 401" in md
    assert "protection" in md.lower()


def test_long_reports_are_capped():
    result = _result([_finding(fp=str(i)) for i in range(40)])
    md = action.render_report(result, "http://x", "never")
    assert "…and 10 more." in md


# ---- outputs -------------------------------------------------------------


def test_outputs_written_in_github_format(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    result = _result([_finding(fp="1"), _finding(Impact.minor, "x", "2")], needs_review=[_finding(fp="3")])

    action.write_outputs(result, "a11y-results.json")

    written = dict(line.split("=", 1) for line in out.read_text().strip().splitlines())
    assert written == {
        "violations": "2",
        "critical": "1",
        "serious": "0",
        "moderate": "0",
        "minor": "1",
        "needs-review": "1",
        "json-file": "a11y-results.json",
    }


def test_outputs_are_a_noop_without_github_output(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    action.write_outputs(_result(), "x.json")  # must not raise when run locally


def test_summary_appends(tmp_path, monkeypatch):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    action.write_summary("## one")
    action.write_summary("## two")
    assert path.read_text().splitlines() == ["## one", "## two"]


# ---- input parsing -------------------------------------------------------


def test_inputs_read_github_action_env(monkeypatch):
    monkeypatch.setenv("INPUT_FAIL_ON", " serious ")
    monkeypatch.setenv("INPUT_JSON_FILE", "r.json")
    assert action.inp("fail-on") == "serious", "hyphens map to underscores, value stripped"
    assert action.inp("json-file") == "r.json"
    assert action.inp("missing", "fallback") == "fallback"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("", False),
        ("no", False),
    ],
)
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("INPUT_SUMMARY", raw)
    assert action.flag("summary", True) is expected


# ---- pull request detection ---------------------------------------------


def test_pr_number_from_event_payload(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert action.pr_number() == 42


def test_pr_number_absent_on_push(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"ref": "refs/heads/main"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert action.pr_number() is None

    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert action.pr_number() is None


def test_comment_is_skipped_without_a_pr(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    action.upsert_comment("body")  # must not raise or post
    assert "skipped" in capsys.readouterr().out
