"""Entry point for the GitHub Action.

Scans one URL with the same engine the hosted app uses, then reports through
whichever channels the workflow asked for: job summary, pull request comment,
JSON artifact, and the process exit code.

What this deliberately does NOT do is map findings back to source. That needs
the pull request diff and a model pass, and it is what the hosted app adds.
The action is the honest free half: it tells you what is wrong and where in the
rendered page, not which line of JSX to change.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

# The scanner lives in worker/ and has no dependency on the hosted app's
# Firestore/GitHub layers, so it can be reused here unchanged.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.config import WorkerSettings
from worker.scanner import Scanner
from worker.schema import ScanResult, ScanStatus

IMPACTS = ("critical", "serious", "moderate", "minor")
COMMENT_MARKER = "<!-- a11y-pr-bot-action -->"
DOCS_URL = "https://github.com/zuhairkhalid229/a11y-pr-bot"


def inp(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", default).strip()


def flag(name: str, default: bool = False) -> bool:
    raw = inp(name, str(default)).lower()
    return raw in ("1", "true", "yes", "on")


def log(message: str) -> None:
    print(message, flush=True)


def notice(level: str, message: str) -> None:
    """GitHub workflow command; renders in the run log and the checks UI."""
    print(f"::{level}::{message}", flush=True)


# --------------------------------------------------------------------------
# waiting for the target
# --------------------------------------------------------------------------


def wait_for_url(url: str, seconds: int) -> str | None:
    """Preview deployments are often still booting when the workflow starts.
    Returns None once reachable, else a description of the last failure."""
    if seconds <= 0:
        return None
    deadline = time.monotonic() + seconds
    last = "not attempted"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": "a11y-pr-bot-action"})
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status < 400:
                    if attempt > 1:
                        log(f"  target answered after {attempt} attempts")
                    return None
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # 401/403 usually means deployment protection rather than "not ready".
            if exc.code in (401, 403):
                return f"HTTP {exc.code} (deployment protection?)"
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(min(5, max(1, seconds // 12)))
    return last


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def counts(result: ScanResult) -> Counter:
    return Counter(f.impact.value for f in result.findings)


def worst_impact(result: ScanResult) -> str | None:
    present = counts(result)
    return next((i for i in IMPACTS if present.get(i)), None)


def should_fail(result: ScanResult, fail_on: str) -> bool:
    if fail_on not in IMPACTS:
        return False
    threshold = IMPACTS.index(fail_on)
    return any(IMPACTS.index(i) <= threshold for i in counts(result))


def render_report(result: ScanResult, url: str, fail_on: str) -> str:
    """Markdown used for both the job summary and the PR comment."""
    if result.status is not ScanStatus.ok:
        return (
            f"## Accessibility scan could not run\n\n"
            f"**{result.error}**\n\n"
            f"Target: `{url}`\n\n"
            "If the deployment is protected (Vercel Authentication is on by default), "
            "the scanner cannot reach it. Make the preview public for the scan, or use "
            f"[the hosted app]({DOCS_URL}), which supports a protection bypass token.\n"
        )

    present = counts(result)
    total = len(result.findings)
    lines: list[str] = []

    if total == 0:
        lines.append("## Accessibility: no WCAG 2.2 AA violations found")
        lines.append("")
        lines.append(f"`{url}` — {result.passes} axe rules passed.")
    else:
        lines.append(f"## Accessibility: {total} violation{'s' if total != 1 else ''}")
        lines.append("")
        lines.append(
            f"`{url}` — scanned with axe-core {result.engine_version} "
            f"at {result.viewport} in {result.duration_ms / 1000:.1f}s"
        )
        lines.append("")
        lines.append("| Impact | Count |")
        lines.append("|---|---:|")
        for impact in IMPACTS:
            if present.get(impact):
                lines.append(f"| {impact} | {present[impact]} |")
        lines.append("")

        by_rule: dict[str, list] = {}
        for finding in result.findings:
            by_rule.setdefault(finding.rule_id, []).append(finding)

        lines.append("| Rule | WCAG | EN 301 549 | Elements |")
        lines.append("|---|---|---|---:|")
        for rule_id, group in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            sample = group[0]
            wcag = ", ".join(c.id for c in sample.wcag) or "—"
            en = ", ".join(sample.en_301_549) or "—"
            lines.append(f"| [{rule_id}]({sample.help_url}) | {wcag} | {en} | {len(group)} |")
        lines.append("")

        lines.append("<details><summary>Failing elements</summary>\n")
        for finding in sorted(result.findings, key=lambda f: IMPACTS.index(f.impact.value))[:30]:
            snippet = finding.html.replace("`", "'")[:160]
            lines.append(f"- **{finding.impact.value}** · {finding.help}  ")
            lines.append(f"  `{finding.selector}`  ")
            lines.append(f"  ```html\n  {snippet}\n  ```")
        if total > 30:
            lines.append(f"\n_…and {total - 30} more._")
        lines.append("\n</details>")

    if result.needs_review:
        n = len(result.needs_review)
        lines.append("")
        lines.append(
            f"{n} element{'s' if n != 1 else ''} could not be decided automatically "
            "and need a human to check."
        )

    if fail_on in IMPACTS:
        lines.append("")
        lines.append(f"_Failing the job on **{fail_on}** impact or higher._")

    lines.append("")
    lines.append(
        "<sub>Automated checks cover roughly a third of WCAG 2.2 success criteria — they cannot "
        "tell you whether alt text is meaningful or whether focus order makes sense. "
        f"[a11y-pr-bot]({DOCS_URL}) · this action reports violations in the rendered page; the "
        "GitHub App traces them back to your JSX and suggests the fix.</sub>"
    )
    return "\n".join(lines)


def write_outputs(result: ScanResult, json_file: str) -> None:
    present = counts(result)
    path = os.environ.get("GITHUB_OUTPUT")
    values = {
        "violations": len(result.findings),
        "critical": present.get("critical", 0),
        "serious": present.get("serious", 0),
        "moderate": present.get("moderate", 0),
        "minor": present.get("minor", 0),
        "needs-review": len(result.needs_review),
        "json-file": json_file,
    }
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


# --------------------------------------------------------------------------
# pull request comment
# --------------------------------------------------------------------------


def pr_number() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return None
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("pull_request", "issue"):
        if isinstance(event.get(key), dict) and event[key].get("number"):
            return int(event[key]["number"])
    return None


def api(method: str, url: str, token: str, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "a11y-pr-bot-action")
    if body:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


def upsert_comment(markdown: str) -> None:
    """One comment per pull request, edited in place on every run. A new comment
    per push is how a bot gets muted."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    number = pr_number()
    if not (token and repo and number):
        notice("notice", "comment: skipped (not a pull request, or no token)")
        return

    base = f"https://api.github.com/repos/{repo}/issues"
    body = f"{COMMENT_MARKER}\n{markdown}"
    try:
        existing = api("GET", f"{base}/{number}/comments?per_page=100", token) or []
        mine = next((c for c in existing if COMMENT_MARKER in (c.get("body") or "")), None)
        if mine:
            api("PATCH", f"{base}/comments/{mine['id']}", token, {"body": body})
            log(f"  updated comment {mine['id']}")
        else:
            created = api("POST", f"{base}/{number}/comments", token, {"body": body})
            log(f"  posted comment {created.get('id')}")
    except urllib.error.HTTPError as exc:
        detail = "permissions: pull-requests: write" if exc.code in (403, 404) else f"HTTP {exc.code}"
        notice("warning", f"could not post the PR comment ({detail})")
    except Exception as exc:
        notice("warning", f"could not post the PR comment ({type(exc).__name__})")


# --------------------------------------------------------------------------


async def run() -> int:
    url = inp("url")
    if not url:
        notice("error", "`url` is required")
        return 1

    fail_on = (inp("fail-on", "never") or "never").lower()
    if fail_on not in (*IMPACTS, "never"):
        notice("error", f"fail-on must be one of never, {', '.join(IMPACTS)} — got {fail_on!r}")
        return 1

    width, _, height = inp("viewport", "1280x800").partition("x")
    settings = WorkerSettings(
        viewport_width=int(width or 1280),
        viewport_height=int(height or 800),
        max_concurrent_scans=1,
    )

    log(f"Scanning {url}")
    if problem := wait_for_url(url, int(inp("wait-for", "60") or 0)):
        notice("warning", f"target never became reachable: {problem}. Scanning anyway.")

    scanner = Scanner(settings)
    await scanner.start()
    try:
        result = await scanner.scan("action", url)
    finally:
        await scanner.stop()

    json_file = inp("json-file", "a11y-results.json")
    Path(json_file).write_text(json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8")

    markdown = render_report(result, url, fail_on)
    write_outputs(result, json_file)
    if flag("summary", True):
        write_summary(markdown)
    if flag("comment", False):
        upsert_comment(markdown)

    if result.status is not ScanStatus.ok:
        notice("error", f"scan failed: {result.error}")
        # A target we could not reach is a workflow problem, not a finding.
        return 1

    total = len(result.findings)
    present = counts(result)
    log(f"  {total} violations  {dict(present)}  ({result.passes} rules passed)")

    if should_fail(result, fail_on):
        notice(
            "error", f"{total} accessibility violations ({worst_impact(result)} and above; fail-on={fail_on})"
        )
        return 1

    if total:
        notice("warning", f"{total} accessibility violations found (not failing; fail-on={fail_on})")
    return 0


def main() -> int:
    import asyncio

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # a crash here must not be a silent green tick
        notice("error", f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
