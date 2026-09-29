"""Post-queue lifecycle with the store faked (real transition function), the
Checks API faked, and a canned scanner. One real-browser test at the end
proves the pieces compose."""

from datetime import UTC, datetime

import pytest

from app.crypto import SecretBox
from app.store.scan_state import transition
from worker.pipeline import MAX_TRANSIENT_ATTEMPTS, Pipeline
from worker.schema import Finding, Impact, ScanResult, ScanStatus, WcagCriterion

SCAN_ID = "555_acme__site_" + "a" * 40
KEY = SecretBox.generate_key()


class FakeStore:
    def __init__(self, doc=None, repo_config=None, plan=None, usage=None):
        self.scans = {SCAN_ID: doc} if doc else {}
        self.findings: dict[str, list] = {}
        self.findings_installation: int | None = None
        self.repo_config = repo_config
        self.plan = plan
        self.usage = usage
        self.counted: list[tuple[str, int]] = []

    async def get_scan(self, scan_id):
        return self.scans.get(scan_id)

    async def transition_scan(self, scan_id, *, allowed_from, to, extra=None):
        updated = transition(self.scans.get(scan_id), allowed_from=allowed_from, to=to)
        if updated is None:
            return False
        self.scans[scan_id] = {**updated, **(extra or {})}
        return True

    async def update_scan(self, scan_id, data):
        self.scans.setdefault(scan_id, {}).update(data)

    async def write_findings(self, scan_id, findings, *, installation_id=None):
        self.findings[scan_id] = findings
        self.findings_installation = installation_id

    async def get_plan(self, installation_id):
        return self.plan

    async def get_usage(self, usage_id):
        return self.usage

    async def count_private_scan(self, usage_id, repo_id):
        self.counted.append((usage_id, repo_id))

    async def get_repo_config(self, installation_id, repo_id):
        return self.repo_config


class FakeChecks:
    def __init__(self):
        self.started: list[int] = []
        self.completed: list[dict] = []

    async def start(self, *, installation_id, repo_full_name, check_run_id):
        self.started.append(check_run_id)
        return True

    async def complete(
        self,
        *,
        installation_id,
        repo_full_name,
        check_run_id,
        conclusion,
        title,
        summary,
        text=None,
        annotations=None,
    ):
        self.completed.append(
            {
                "id": check_run_id,
                "conclusion": conclusion,
                "title": title,
                "summary": summary,
                "annotations": annotations,
            }
        )
        return True


class FakeScanner:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def scan(self, scan_id, url, *, extra_headers=None):
        self.calls.append((url, dict(extra_headers or {})))
        return self.result


def _doc(state="queued", check_run_id=4242):
    return {
        "installation_id": 555,
        "repo_id": 99,
        "repo_full_name": "acme/site",
        "head_sha": "a" * 40,
        "state": state,
        "check_run_id": check_run_id,
        "pr": {"number": 7},
        "preview": {"url": "https://site-abc.vercel.app", "provider": "vercel"},
    }


def _result(status=ScanStatus.ok, findings=(), error=None):
    now = datetime.now(UTC)
    return ScanResult(
        scan_id=SCAN_ID,
        status=status,
        error=error,
        requested_url="https://site-abc.vercel.app",
        final_url="https://site-abc.vercel.app/",
        engine_version="4.13.0",
        started_at=now,
        finished_at=now,
        duration_ms=900,
        findings=list(findings),
        passes=12,
        counts_by_impact={f.impact.value: 1 for f in findings},
    )


def _finding():
    return Finding(
        fingerprint="deadbeef00000001",
        rule_id="image-alt",
        impact=Impact.critical,
        wcag=[WcagCriterion(id="1.1.1", name="Non-text Content", level="A", introduced_in="2.0")],
        en_301_549=["9.1.1.1"],
        page_url="https://site-abc.vercel.app/",
        page_path="/",
        selector="img",
        html="<img src=x>",
        failure_summary="no alt",
        help="Images must have alternative text",
        help_url="https://dequeuniversity.com/rules/axe/4.13/image-alt",
    )


def _pipeline(store, scanner, secrets=None):
    checks = FakeChecks()
    return Pipeline(store=store, checks=checks, scanner=scanner, secrets=secrets or SecretBox("")), checks


async def test_happy_path_completes_with_findings():
    store = FakeStore(_doc())
    scanner = FakeScanner(_result(findings=[_finding()]))
    pipeline, checks = _pipeline(store, scanner)

    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)

    assert retry is False and result.status is ScanStatus.ok
    assert store.scans[SCAN_ID]["state"] == "completed"
    assert store.scans[SCAN_ID]["result"]["findings"] == 1
    assert store.scans[SCAN_ID]["attempts"] == 1
    assert store.findings[SCAN_ID][0]["fingerprint"] == "deadbeef00000001"
    assert checks.started == [4242]
    assert checks.completed[0]["conclusion"] == "neutral"
    assert "1 accessibility issue" in checks.completed[0]["title"]
    assert "image-alt" in checks.completed[0]["summary"]
    assert "9.1.1.1" in checks.completed[0]["summary"]


async def test_clean_scan_is_success():
    store = FakeStore(_doc())
    pipeline, checks = _pipeline(store, FakeScanner(_result()))
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert checks.completed[0]["conclusion"] == "success"
    assert store.scans[SCAN_ID]["state"] == "completed"


async def test_permanent_failure_is_failed_and_not_retried():
    store = FakeStore(_doc())
    pipeline, checks = _pipeline(
        store, FakeScanner(_result(ScanStatus.failed_permanent, error="target returned HTTP 401"))
    )
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert retry is False
    assert store.scans[SCAN_ID]["state"] == "failed"
    assert checks.completed[0]["conclusion"] == "neutral"
    assert "Bypass" in checks.completed[0]["summary"]


async def test_transient_failure_retries_and_stays_in_progress():
    store = FakeStore(_doc())
    pipeline, checks = _pipeline(
        store, FakeScanner(_result(ScanStatus.failed_transient, error="navigation timed out"))
    )
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert retry is True
    assert store.scans[SCAN_ID]["state"] == "in_progress"
    assert store.scans[SCAN_ID]["last_error"] == "navigation timed out"
    assert checks.completed == []

    # Retry arrives: Check Run is not re-started, still in_progress is accepted.
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=1)
    assert retry is True
    assert checks.started == [4242], "start() only on the first attempt"


async def test_transient_failure_exhausted_becomes_failed():
    store = FakeStore(_doc())
    pipeline, checks = _pipeline(
        store, FakeScanner(_result(ScanStatus.failed_transient, error="navigation timed out"))
    )
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=MAX_TRANSIENT_ATTEMPTS - 1)
    assert retry is False
    assert store.scans[SCAN_ID]["state"] == "failed"
    assert f"gave up after {MAX_TRANSIENT_ATTEMPTS} attempts" in checks.completed[0]["summary"]


@pytest.mark.parametrize("state", ["completed", "failed", "no_preview", "awaiting_deployment"])
async def test_scan_in_wrong_state_is_skipped(state):
    store = FakeStore(_doc(state=state))
    scanner = FakeScanner(_result())
    pipeline, checks = _pipeline(store, scanner)
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert result is None and retry is False
    assert scanner.calls == [] and checks.started == []
    assert store.scans[SCAN_ID]["state"] == state


async def test_missing_scan_is_skipped():
    pipeline, _ = _pipeline(FakeStore(), FakeScanner(_result()))
    assert await pipeline.run_scan("nope", retry_count=0) == (None, False)


async def test_bypass_secret_is_decrypted_into_headers_and_never_stored_plain():
    box = SecretBox(KEY)
    store = FakeStore(_doc(), repo_config={"vercel_bypass_secret": box.encrypt("s3cret-token")})
    scanner = FakeScanner(_result())
    pipeline, _ = _pipeline(store, scanner, secrets=box)
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    _url, headers = scanner.calls[0]
    assert headers == {"x-vercel-protection-bypass": "s3cret-token"}
    assert "s3cret-token" not in str(store.repo_config)


async def test_undecryptable_secret_is_dropped_not_sent():
    store = FakeStore(_doc(), repo_config={"vercel_bypass_secret": "garbage"})
    scanner = FakeScanner(_result())
    pipeline, _ = _pipeline(store, scanner, secrets=SecretBox(KEY))
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert scanner.calls[0][1] == {}


async def test_no_check_run_id_still_completes_state():
    store = FakeStore(_doc(check_run_id=None))
    pipeline, checks = _pipeline(store, FakeScanner(_result()))
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert store.scans[SCAN_ID]["state"] == "completed"
    assert checks.started == [] and checks.completed == []


# ---- fallback ------------------------------------------------------------


async def test_fallback_retires_waiting_scan():
    store = FakeStore(_doc(state="awaiting_deployment"))
    pipeline, checks = _pipeline(store, FakeScanner(_result()))
    assert await pipeline.run_fallback(SCAN_ID) is True
    assert store.scans[SCAN_ID]["state"] == "no_preview"
    assert checks.completed[0]["conclusion"] == "neutral"
    assert "Deployments: read" in checks.completed[0]["summary"]


@pytest.mark.parametrize(
    "state", ["awaiting_pr", "queued", "in_progress", "completed", "failed", "no_preview"]
)
async def test_fallback_is_noop_once_scan_moved_on(state):
    store = FakeStore(_doc(state=state))
    pipeline, checks = _pipeline(store, FakeScanner(_result()))
    assert await pipeline.run_fallback(SCAN_ID) is False
    assert store.scans[SCAN_ID]["state"] == state
    assert checks.completed == []


async def test_fallback_for_unknown_scan():
    pipeline, _ = _pipeline(FakeStore(), FakeScanner(_result()))
    assert await pipeline.run_fallback("nope") is False


# ---- with a real browser -------------------------------------------------


@pytest.mark.browser
async def test_pipeline_end_to_end_with_real_scanner(fixture_server):
    from worker.config import WorkerSettings
    from worker.scanner import Scanner

    doc = _doc()
    doc["preview"]["url"] = f"{fixture_server}/inaccessible.html"
    store = FakeStore(doc)
    scanner = Scanner(WorkerSettings())
    await scanner.start()
    try:
        pipeline, checks = _pipeline(store, scanner)
        result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    finally:
        await scanner.stop()

    assert retry is False
    assert store.scans[SCAN_ID]["state"] == "completed"
    assert store.scans[SCAN_ID]["result"]["findings"] >= 7
    assert len(store.findings[SCAN_ID]) == len(result.findings) + len(result.needs_review)
    assert checks.completed[0]["conclusion"] == "neutral"
    assert "| critical |" in checks.completed[0]["summary"]


# ---- Day 4: mapping is wired in and never fails the scan ---------------------


class _FakeGitHub:
    """fetch_pr_files calls .request; give it the two shapes it needs."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    async def request(self, method, path, installation_id, *, json=None):
        import base64

        import httpx

        if "/pulls/" in path and "/files" in path:
            body = [
                {
                    "filename": p,
                    "status": "modified",
                    "patch": "@@ -1,%d +1,%d @@\n" % (n, n) + "\n".join(" " + l for l in c.splitlines()),
                }
                for p, c in self._files.items()
                for n in [len(c.splitlines())]
            ]
            return httpx.Response(200, json=body)
        if "/contents/" in path:
            p = path.split("/contents/")[1].split("?")[0]
            c = self._files[p]
            return httpx.Response(
                200,
                json={"encoding": "base64", "size": len(c), "content": base64.b64encode(c.encode()).decode()},
            )
        return httpx.Response(404)


class _NoMatchModel:
    model_id = "fake"

    async def map(self, prompt):
        from worker.mapper.gemini import MapperResponse

        return MapperResponse(matched=False, location_confidence=0, patch_confidence=0, rationale="no")


async def test_mapper_runs_and_disposition_is_persisted():
    from worker.mapper.mapper import SourceMapper

    store = FakeStore(_doc())
    checks = FakeChecks()
    pipeline = Pipeline(
        store=store,
        checks=checks,
        scanner=FakeScanner(_result(findings=[_finding()])),
        secrets=SecretBox(""),
        github=_FakeGitHub({"src/A.tsx": "<img src=x />\n"}),
        mapper=SourceMapper(_NoMatchModel()),
    )
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    persisted = store.findings[SCAN_ID][0]
    assert persisted["disposition"] == "drop"
    assert store.scans[SCAN_ID]["result"]["mapping"]["model_calls"] == 1
    assert store.scans[SCAN_ID]["state"] == "completed"


async def test_mapping_disabled_marks_findings_and_scan_still_completes():
    store = FakeStore(_doc())
    pipeline, checks = _pipeline(store, FakeScanner(_result(findings=[_finding()])))
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert store.findings[SCAN_ID][0]["disposition_reason"] == "source mapping disabled"
    assert store.scans[SCAN_ID]["result"]["mapping"] is None
    assert checks.completed[0]["conclusion"] == "neutral"


async def test_mapping_exception_never_fails_scan():
    from worker.mapper.mapper import SourceMapper

    class Boom:
        async def request(self, *a, **k):
            raise RuntimeError("github down")

    store = FakeStore(_doc())
    checks = FakeChecks()
    pipeline = Pipeline(
        store=store,
        checks=checks,
        scanner=FakeScanner(_result(findings=[_finding()])),
        secrets=SecretBox(""),
        github=Boom(),
        mapper=SourceMapper(_NoMatchModel()),
    )
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert retry is False
    assert store.scans[SCAN_ID]["state"] == "completed"
    assert store.findings[SCAN_ID][0]["disposition_reason"] == "mapping error"


# ---- Day 5: posting is wired in, annotations reach the Check Run --------------


class _FakePoster:
    def __init__(self, annotations=(), raise_=False):
        self.calls = 0
        self.annotations = list(annotations)
        self.raise_ = raise_

    async def post(self, doc, result):
        from worker.poster.plan import PostPlan
        from worker.poster.poster import PostOutcome

        self.calls += 1
        if self.raise_:
            raise RuntimeError("github down")
        out = PostOutcome(plan=PostPlan())
        out.posted = 2
        out.check_annotations = self.annotations
        return out


async def test_poster_runs_and_annotations_reach_check_run():
    store = FakeStore(_doc())
    checks = FakeChecks()
    poster = _FakePoster(
        annotations=[
            {
                "path": "src/A.tsx",
                "start_line": 3,
                "end_line": 3,
                "annotation_level": "warning",
                "title": "t",
                "message": "m",
            }
        ]
    )
    pipeline = Pipeline(
        store=store,
        checks=checks,
        scanner=FakeScanner(_result(findings=[_finding()])),
        secrets=SecretBox(""),
        poster=poster,
    )
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert poster.calls == 1
    assert checks.completed[0]["annotations"][0]["path"] == "src/A.tsx"
    assert "2 review comments posted" in checks.completed[0]["summary"]
    assert store.scans[SCAN_ID]["result"]["posting"]["posted"] == 2


async def test_poster_not_called_for_clean_scan():
    poster = _FakePoster()
    pipeline = Pipeline(
        store=FakeStore(_doc()),
        checks=FakeChecks(),
        scanner=FakeScanner(_result()),
        secrets=SecretBox(""),
        poster=poster,
    )
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert poster.calls == 0


async def test_poster_exception_never_fails_scan():
    store = FakeStore(_doc())
    checks = FakeChecks()
    pipeline = Pipeline(
        store=store,
        checks=checks,
        scanner=FakeScanner(_result(findings=[_finding()])),
        secrets=SecretBox(""),
        poster=_FakePoster(raise_=True),
    )
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert retry is False and store.scans[SCAN_ID]["state"] == "completed"
    assert checks.completed[0]["annotations"] is None


# ---- Day 6: quota gate -------------------------------------------------------


async def test_public_repo_is_never_metered():
    """Public repos are the distribution engine; they must not touch the meter."""
    store = FakeStore(
        {**_doc(), "repo_private": False},
        plan="free",
        usage={"private_scans_run": 9999, "private_repo_ids": [1, 2, 3]},
    )
    scanner = FakeScanner(_result())
    pipeline, _checks = _pipeline(store, scanner)
    result, _retry = await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert result is not None and scanner.calls, "public scan must proceed"
    assert store.counted == [], "no meter bump for public repos"
    assert store.scans[SCAN_ID]["state"] == "completed"


async def test_private_repo_within_limits_runs_and_counts():
    store = FakeStore(
        {**_doc(), "repo_private": True},
        plan="free",
        usage={"private_scans_run": 10, "private_repo_ids": [99]},
    )
    scanner = FakeScanner(_result())
    pipeline, _ = _pipeline(store, scanner)
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert scanner.calls
    assert store.counted == [(f"555_{datetime.now(UTC):%Y%m}", 99)]


async def test_private_repo_limit_skips_scan_with_check_run_message():
    store = FakeStore(
        {**_doc(), "repo_private": True},
        plan="free",
        usage={"private_scans_run": 0, "private_repo_ids": [12345]},
    )
    scanner = FakeScanner(_result())
    pipeline, checks = _pipeline(store, scanner)
    result, retry = await pipeline.run_scan(SCAN_ID, retry_count=0)

    assert (result, retry) == (None, False)
    assert scanner.calls == [], "no browser launched for an over-quota scan"
    assert store.scans[SCAN_ID]["state"] == "quota_exceeded"
    assert store.scans[SCAN_ID]["quota_reason"] == "private_repo_limit"
    assert store.counted == []
    assert checks.started == [], "no in_progress flicker"
    assert checks.completed[0]["conclusion"] == "neutral"
    assert checks.completed[0]["title"] == "Scan skipped — plan limit reached"
    assert "1 private repositor" in checks.completed[0]["summary"]


async def test_monthly_scan_limit_skips_scan():
    store = FakeStore(
        {**_doc(), "repo_private": True},
        plan="free",
        usage={"private_scans_run": 50, "private_repo_ids": [99]},
    )
    scanner = FakeScanner(_result())
    pipeline, checks = _pipeline(store, scanner)
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert scanner.calls == []
    assert store.scans[SCAN_ID]["quota_reason"] == "scan_limit"
    assert "50/50" in checks.completed[0]["summary"]


async def test_paid_plan_raises_the_ceiling():
    store = FakeStore(
        {**_doc(), "repo_private": True},
        plan="pro",
        usage={"private_scans_run": 50, "private_repo_ids": [1, 2, 99]},
    )
    scanner = FakeScanner(_result())
    pipeline, _ = _pipeline(store, scanner)
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert scanner.calls, "pro allows 5 repos / 500 scans"


async def test_team_plan_is_unmetered_but_still_counted():
    store = FakeStore(
        {**_doc(), "repo_private": True},
        plan="team",
        usage={"private_scans_run": 99999, "private_repo_ids": list(range(50))},
    )
    scanner = FakeScanner(_result())
    pipeline, _ = _pipeline(store, scanner)
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert scanner.calls and store.counted, "unlimited still records usage for the dashboard"


async def test_findings_carry_installation_id_for_security_rules():
    store = FakeStore(_doc())
    pipeline, _ = _pipeline(store, FakeScanner(_result(findings=[_finding()])))
    await pipeline.run_scan(SCAN_ID, retry_count=0)
    assert store.findings_installation == 555
