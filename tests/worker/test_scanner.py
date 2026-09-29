"""Runs real Chromium. `playwright install chromium` first."""

import pytest
from fastapi.testclient import TestClient

from worker.config import WorkerSettings
from worker.main import app
from worker.scanner import Scanner
from worker.schema import ScanStatus

pytestmark = pytest.mark.browser


@pytest.fixture
async def scanner():
    # Function-scoped: a fresh browser per test costs ~1s and sidesteps
    # pytest-asyncio loop-scope mismatches for module-scoped async fixtures.
    s = Scanner(WorkerSettings())
    await s.start()
    yield s
    await s.stop()


async def test_inaccessible_fixture_yields_expected_rules(scanner, fixture_server):
    result = await scanner.scan("t-1", f"{fixture_server}/inaccessible.html")

    assert result.status is ScanStatus.ok, result.error
    assert result.engine_version == "4.13.0"

    rules = {f.rule_id for f in result.findings}
    expected = {
        "html-has-lang",
        "document-title",
        "image-alt",
        "label",
        "button-name",
        "link-name",
        "color-contrast",
    }
    assert expected <= rules, f"missing: {expected - rules}"

    # Two images without alt -> two findings under one rule, distinct fingerprints.
    image_alt = [f for f in result.findings if f.rule_id == "image-alt"]
    assert len(image_alt) == 2
    assert image_alt[0].fingerprint != image_alt[1].fingerprint
    assert len({f.fingerprint for f in result.findings}) == len(result.findings)


async def test_every_finding_is_mapped_to_standards(scanner, fixture_server):
    result = await scanner.scan("t-2", f"{fixture_server}/inaccessible.html")
    for f in result.findings:
        assert f.wcag, f"{f.rule_id} has no WCAG criterion"
        assert f.en_301_549, f"{f.rule_id} has no EN 301 549 clause"
        assert f.selector and f.html and f.failure_summary and f.help_url
        assert f.page_path == "/inaccessible.html"
        assert f.source is None and f.patch is None and f.posted is None

    by_rule = {f.rule_id: f for f in result.findings}
    assert by_rule["image-alt"].wcag[0].id == "1.1.1"
    assert "9.1.1.1" in by_rule["image-alt"].en_301_549
    assert by_rule["color-contrast"].wcag[0].id == "1.4.3"


async def test_incomplete_results_land_in_needs_review(scanner, fixture_server):
    result = await scanner.scan("t-3", f"{fixture_server}/inaccessible.html")
    review_rules = {f.rule_id for f in result.needs_review}
    # Text over a background image: axe cannot decide contrast.
    assert "color-contrast" in review_rules
    hero = [f for f in result.needs_review if f.rule_id == "color-contrast" and "hero" in f.html]
    assert hero, "the .hero paragraph should be flagged for review"


async def test_fingerprint_stable_across_hosts_for_same_page(scanner, fixture_server):
    """Same fixture served on the same server twice: identical fingerprints.
    (Different hosts are covered by the unit test; here we prove the scan
    pipeline itself is deterministic.)"""
    a = await scanner.scan("t-4a", f"{fixture_server}/inaccessible.html")
    b = await scanner.scan("t-4b", f"{fixture_server}/inaccessible.html")
    assert {f.fingerprint for f in a.findings} == {f.fingerprint for f in b.findings}


async def test_accessible_fixture_is_clean(scanner, fixture_server):
    result = await scanner.scan("t-5", f"{fixture_server}/accessible.html")
    assert result.status is ScanStatus.ok
    assert result.findings == [], [f.rule_id for f in result.findings]
    assert result.passes > 0


async def test_404_is_permanent_failure(scanner, fixture_server):
    result = await scanner.scan("t-6", f"{fixture_server}/nope.html")
    assert result.status is ScanStatus.failed_permanent
    assert "404" in result.error


async def test_connection_refused_is_permanent_failure(scanner):
    result = await scanner.scan("t-7", "http://127.0.0.1:9/")  # discard port, nothing listens
    assert result.status is ScanStatus.failed_permanent


async def test_hanging_target_is_transient_failure(scanner, fixture_server):
    result = await scanner.scan("t-8", f"{fixture_server}/hang")
    assert result.status is ScanStatus.failed_transient
    assert "timed out" in result.error


# ---- HTTP layer: what Cloud Tasks actually sees --------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_endpoint_ok_scan_returns_200_with_findings(client, fixture_server):
    r = client.post("/tasks/scan", json={"scan_id": "e-1", "url": f"{fixture_server}/inaccessible.html"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert any(f["rule_id"] == "image-alt" for f in body["findings"])


def test_endpoint_permanent_failure_is_200_so_tasks_does_not_retry(client, fixture_server):
    r = client.post("/tasks/scan", json={"scan_id": "e-2", "url": f"{fixture_server}/nope.html"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed_permanent"


def test_endpoint_transient_failure_is_503_so_tasks_retries(client, fixture_server):
    r = client.post("/tasks/scan", json={"scan_id": "e-3", "url": f"{fixture_server}/hang"})
    assert r.status_code == 503
    assert r.json()["status"] == "failed_transient"


def test_endpoint_rejects_bad_payload(client):
    r = client.post("/tasks/scan", json={"scan_id": "e-4", "url": "not a url"})
    assert r.status_code == 422


class _FakePipeline:
    def __init__(self):
        self.scan_calls: list[tuple[str, int]] = []
        self.fallback_calls: list[str] = []
        self.retry = False

    async def run_scan(self, scan_id, *, retry_count, extra_headers=None):
        self.scan_calls.append((scan_id, retry_count))
        return None, self.retry

    async def run_fallback(self, scan_id):
        self.fallback_calls.append(scan_id)
        return True


def test_endpoint_routes_persisted_scans_to_pipeline_with_retry_count(client):
    fake = client.app.state.pipeline = _FakePipeline()
    r = client.post(
        "/tasks/scan",
        json={
            "scan_id": "e-5",
            "installation_id": 555,
            "repo_id": 99,
            "repo_full_name": "acme/site",
            "pr_number": 7,
            "head_sha": "a" * 40,
            "check_run_id": 4242,
        },
        headers={"X-CloudTasks-TaskRetryCount": "2"},
    )
    assert r.status_code == 200
    assert fake.scan_calls == [("e-5", 2)]

    fake.retry = True
    r = client.post("/tasks/scan", json={"scan_id": "e-6", "installation_id": 555})
    assert r.status_code == 503, "retry=True must surface as 503 for Cloud Tasks"


def test_endpoint_fallback_routes_to_pipeline(client):
    fake = client.app.state.pipeline = _FakePipeline()
    r = client.post("/tasks/fallback", json={"scan_id": "e-7"})
    assert r.status_code == 200 and r.json() == {"scan_id": "e-7", "retired": True}
    assert fake.fallback_calls == ["e-7"]


def test_endpoint_standalone_requires_url(client):
    r = client.post("/tasks/scan", json={"scan_id": "e-8"})
    assert r.status_code == 422
