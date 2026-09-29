"""Playwright + axe-core scan of one URL.

One Browser per process, launched at startup. Each scan gets its own
BrowserContext -- fresh cookies, storage and cache -- so a preview deploy that
sets a session cookie cannot bleed into the next scan.

Failure classification is the important design decision here. Cloud Tasks
retries on non-2xx, so the HTTP layer needs to know whether trying again could
possibly help:

    transient  -> timeout, connection reset, browser crash        -> retry
    permanent  -> 4xx from the target, DNS failure, bad URL       -> do not
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from worker.axe import load_axe
from worker.config import WorkerSettings
from worker.schema import (
    Finding,
    Impact,
    ScanResult,
    ScanStatus,
    compute_fingerprint,
    page_path_of,
)
from worker.wcag import RUN_ONLY_TAGS, criteria_from_tags, en_clauses_from_tags

log = structlog.get_logger(__name__)

_HTML_TRUNCATE = 2000

# Runs inside the page. `selectors` + `ancestry` asks axe for both the
# nth-child path and an ancestry path; the latter survives sibling insertion.
_AXE_RUN_JS = """async (runOnly) => {
    return await axe.run(document, {
        runOnly: { type: 'tag', values: runOnly },
        resultTypes: ['violations', 'incomplete', 'passes', 'inapplicable'],
        selectors: true,
        ancestry: true,
    });
}"""


class ScanError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Scanner:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scans)
        self._axe_source, self._axe_version = load_axe()

    @property
    def axe_version(self) -> str:
        return self._axe_version

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=self._settings.chromium_args
        )
        log.info("browser_started", axe_version=self._axe_version)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def scan(
        self,
        scan_id: str,
        url: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> ScanResult:
        """Scan one URL. Target-site problems come back as a failed ScanResult,
        never as an exception -- the caller decides what to tell Cloud Tasks."""
        started = datetime.now(UTC)
        async with self._semaphore:
            try:
                raw, final_url, title = await self._run_axe(url, extra_headers or {})
            except ScanError as exc:
                finished = datetime.now(UTC)
                log.warning(
                    "scan_failed",
                    scan_id=scan_id,
                    url=url,
                    error=str(exc),
                    retryable=exc.retryable,
                )
                return ScanResult(
                    scan_id=scan_id,
                    status=ScanStatus.failed_transient if exc.retryable else ScanStatus.failed_permanent,
                    error=str(exc),
                    requested_url=url,
                    engine_version=self._axe_version,
                    started_at=started,
                    finished_at=finished,
                    duration_ms=int((finished - started).total_seconds() * 1000),
                )

        finished = datetime.now(UTC)
        result = self._to_result(scan_id, url, final_url, title, raw, started, finished)
        log.info(
            "scan_complete",
            scan_id=scan_id,
            url=url,
            findings=len(result.findings),
            needs_review=len(result.needs_review),
            duration_ms=result.duration_ms,
        )
        return result

    # ------------------------------------------------------------------

    async def _run_axe(self, url: str, extra_headers: dict[str, str]) -> tuple[dict[str, Any], str, str]:
        assert self._browser is not None, "Scanner.start() not called"
        settings = self._settings

        context = await self._browser.new_context(
            viewport={"width": settings.viewport_width, "height": settings.viewport_height},
            user_agent=settings.user_agent,
            extra_http_headers=extra_headers,
        )
        try:
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="load", timeout=settings.navigation_timeout_ms)
            except PlaywrightTimeout as exc:
                raise ScanError(
                    f"navigation timed out after {settings.navigation_timeout_ms}ms",
                    retryable=True,
                ) from exc
            except PlaywrightError as exc:
                # net::ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_REFUSED, malformed
                # URL. A preview host that does not resolve will not start
                # resolving because we asked again.
                raise ScanError(f"navigation failed: {_first_line(exc.message)}", retryable=False) from exc

            if response is None:
                raise ScanError("no response (about:blank navigation)", retryable=False)
            if response.status >= 400:
                # 401/403 is almost always Vercel/Netlify deployment protection.
                # Day 3 passes the bypass token through extra_headers.
                raise ScanError(
                    f"target returned HTTP {response.status}",
                    retryable=response.status >= 500,
                )

            # Best effort: let hydration and initial fetches finish. SPAs that
            # poll never go idle, so a timeout here is not a failure.
            try:
                await page.wait_for_load_state("networkidle", timeout=settings.settle_timeout_ms)
            except PlaywrightTimeout:
                pass

            await page.add_script_tag(content=self._axe_source)
            try:
                raw = await asyncio.wait_for(
                    page.evaluate(_AXE_RUN_JS, RUN_ONLY_TAGS),
                    timeout=settings.axe_timeout_ms / 1000,
                )
            except TimeoutError as exc:
                raise ScanError(f"axe.run exceeded {settings.axe_timeout_ms}ms", retryable=True) from exc
            except PlaywrightError as exc:
                raise ScanError(f"axe.run failed: {_first_line(exc.message)}", retryable=True) from exc

            return raw, page.url, await page.title()
        finally:
            await context.close()

    def _to_result(
        self,
        scan_id: str,
        requested_url: str,
        final_url: str,
        title: str,
        raw: dict[str, Any],
        started: datetime,
        finished: datetime,
    ) -> ScanResult:
        page_path = page_path_of(final_url)
        findings = self._flatten(raw.get("violations", []), final_url, page_path)
        needs_review = self._flatten(raw.get("incomplete", []), final_url, page_path)

        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.impact.value] = counts.get(finding.impact.value, 0) + 1

        return ScanResult(
            scan_id=scan_id,
            status=ScanStatus.ok,
            requested_url=requested_url,
            final_url=final_url,
            page_title=title or None,
            engine_version=self._axe_version,
            viewport=f"{self._settings.viewport_width}x{self._settings.viewport_height}",
            started_at=started,
            finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000),
            findings=findings,
            needs_review=needs_review,
            passes=len(raw.get("passes", [])),
            inapplicable=len(raw.get("inapplicable", [])),
            counts_by_impact=counts,
        )

    @staticmethod
    def _flatten(rules: list[dict[str, Any]], page_url: str, page_path: str) -> list[Finding]:
        """axe groups by rule, each with N nodes. We want one Finding per node:
        that is the unit a PR comment attaches to."""
        findings: list[Finding] = []
        for rule in rules:
            tags = rule.get("tags", [])
            wcag = criteria_from_tags(tags)
            en = en_clauses_from_tags(tags)
            for node in rule.get("nodes", []):
                target = node.get("target") or ["<unknown>"]
                # target is a list to express shadow DOM / iframe boundaries.
                selector = " >>> ".join(str(t) for t in target)
                html = (node.get("html") or "")[:_HTML_TRUNCATE]
                impact = node.get("impact") or rule.get("impact") or "moderate"
                findings.append(
                    Finding(
                        fingerprint=compute_fingerprint(rule["id"], page_path, html),
                        rule_id=rule["id"],
                        impact=Impact(impact),
                        wcag=wcag,
                        en_301_549=en,
                        page_url=page_url,
                        page_path=page_path,
                        selector=selector,
                        html=html,
                        failure_summary=node.get("failureSummary") or rule.get("description", ""),
                        help=rule.get("help", ""),
                        help_url=rule.get("helpUrl", ""),
                    )
                )
        return findings


def _first_line(message: str) -> str:
    return message.splitlines()[0] if message else "unknown error"
