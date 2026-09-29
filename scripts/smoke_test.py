"""End-to-end smoke test: open a PR with a deliberate accessibility bug and
watch every stage of the pipeline, naming the one that broke.

    export GH_TOKEN=$(gh auth token)      # a user token; opens the PR
    python scripts/smoke_test.py --repo owner/repo

Reads Firestore with your ADC credentials (`gcloud auth application-default
login`) and GitHub with two identities on purpose: GH_TOKEN opens the PR as a
human would, and the app's own JWT reads what the app can see. A bug that only
appears under one identity is exactly the kind this is meant to catch.

Each stage prints PASS/FAIL with the specific thing to check. The run stops at
the first failure -- later stages cannot pass and their errors would be noise.

    --keep      leave the branch and PR behind for inspection
    --repo      owner/repo the app is installed on, with working previews
    --timeout   seconds to wait for the preview+scan stages (default 420)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import get_settings
from app.github.auth import GitHubAuth
from app.store.firestore import Store, scan_id_for

GITHUB = "https://api.github.com"
BRANCH = f"a11y-smoke-{int(time.time())}"
FIXTURE_PATH = "a11y-smoke-test.html"

# One deliberate, unambiguous violation: axe rule image-alt, WCAG 1.1.1. A real
# .html file at the repo root is scanned by any static host and by every
# framework's public directory, which keeps this test independent of the repo's
# build setup.
FIXTURE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>a11y smoke test</title></head>
<body>
  <main>
    <h1>Smoke test</h1>
    <!-- Deliberate WCAG 1.1.1 failure: no alt attribute. -->
    <img src="/a11y-smoke-test-pixel.gif" width="40" height="40">
  </main>
</body>
</html>
"""


class StageFailure(Exception):
    def __init__(self, message: str, hints: list[str]) -> None:
        super().__init__(message)
        self.hints = hints


@dataclass
class Context:
    repo: str
    timeout: float
    installation_id: int = 0
    pr_number: int = 0
    head_sha: str = ""
    scan_id: str = ""
    check_run_id: int | None = None
    notes: list[str] = field(default_factory=list)


class Smoke:
    def __init__(self, ctx: Context, gh_token: str) -> None:
        self.ctx = ctx
        self.settings = get_settings()
        self.http = httpx.AsyncClient(timeout=30.0)
        self.user_headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.auth = GitHubAuth(self.settings, self.http)
        self.store = Store(self.settings)

    async def close(self) -> None:
        await self.http.aclose()
        await self.store.close()

    # -- helpers ------------------------------------------------------------

    async def _app(self, method: str, path: str, **kw) -> httpx.Response:
        """Call GitHub as the app installation."""
        token = await self.auth.installation_token(self.ctx.installation_id)
        headers = {**self.user_headers, "Authorization": f"Bearer {token}"}
        return await self.http.request(method, f"{GITHUB}{path}", headers=headers, **kw)

    async def _user(self, method: str, path: str, **kw) -> httpx.Response:
        return await self.http.request(method, f"{GITHUB}{path}", headers=self.user_headers, **kw)

    async def _poll(self, predicate, *, what: str, timeout: float, hints: list[str], interval: float = 5.0):
        """Poll until predicate returns something truthy, else raise."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = await predicate()
            if last:
                return last
            await asyncio.sleep(interval)
        raise StageFailure(f"timed out after {timeout:.0f}s waiting for {what}", hints)

    # -- stages -------------------------------------------------------------

    async def stage_app_installed(self) -> str:
        """The app can see the repo, and its JWT is valid."""
        jwt_headers = {**self.user_headers, "Authorization": f"Bearer {self.auth.app_jwt()}"}
        r = await self.http.get(f"{GITHUB}/repos/{self.ctx.repo}/installation", headers=jwt_headers)
        if r.status_code == 401:
            raise StageFailure(
                "GitHub rejected the app JWT",
                [
                    "GITHUB_APP_ID does not match the private key, or the key is malformed.",
                    "Check GITHUB_PRIVATE_KEY survived the env round trip (base64 or \\n-escaped PEM).",
                    "Check your machine clock: a JWT more than 60s in the future is rejected.",
                ],
            )
        if r.status_code == 404:
            raise StageFailure(
                f"the app is not installed on {self.ctx.repo}",
                [
                    "Install it: https://github.com/apps/a11y-pr-bot/installations/new",
                    "If it is installed org-wide, confirm this repository is selected.",
                ],
            )
        r.raise_for_status()
        self.ctx.installation_id = r.json()["id"]

        repo = (await self._user("GET", f"/repos/{self.ctx.repo}")).json()
        if repo.get("private"):
            self.ctx.notes.append("private repo: this scan counts against the monthly quota")
        return f"installation {self.ctx.installation_id}, default branch {repo['default_branch']}"

    async def stage_branch_pushed(self) -> str:
        repo = (await self._user("GET", f"/repos/{self.ctx.repo}")).json()
        base = repo["default_branch"]
        ref = (await self._user("GET", f"/repos/{self.ctx.repo}/git/ref/heads/{base}")).json()

        r = await self._user(
            "POST",
            f"/repos/{self.ctx.repo}/git/refs",
            json={"ref": f"refs/heads/{BRANCH}", "sha": ref["object"]["sha"]},
        )
        if r.status_code not in (200, 201):
            raise StageFailure(
                f"could not create branch ({r.status_code}: {r.text[:200]})",
                [
                    "GH_TOKEN needs write access to this repository.",
                    "A branch protection rule may forbid ref creation.",
                ],
            )

        r = await self._user(
            "PUT",
            f"/repos/{self.ctx.repo}/contents/{FIXTURE_PATH}",
            json={
                "message": "test: accessibility smoke test fixture",
                "content": base64.b64encode(FIXTURE.encode()).decode(),
                "branch": BRANCH,
            },
        )
        if r.status_code not in (200, 201):
            raise StageFailure(
                f"could not commit the fixture ({r.status_code}: {r.text[:200]})",
                [
                    "GH_TOKEN lacks contents:write.",
                ],
            )
        self.ctx.head_sha = r.json()["commit"]["sha"]
        self.ctx.scan_id = scan_id_for(self.ctx.installation_id, self.ctx.repo, self.ctx.head_sha)
        return f"branch {BRANCH} at {self.ctx.head_sha[:7]}"

    async def stage_pr_opened(self) -> str:
        repo = (await self._user("GET", f"/repos/{self.ctx.repo}")).json()
        r = await self._user(
            "POST",
            f"/repos/{self.ctx.repo}/pulls",
            json={
                "title": "test: accessibility smoke test",
                "head": BRANCH,
                "base": repo["default_branch"],
                "body": "Automated smoke test for the accessibility bot. Safe to close.",
            },
        )
        if r.status_code != 201:
            raise StageFailure(
                f"could not open the PR ({r.status_code}: {r.text[:200]})",
                [
                    "GH_TOKEN needs pull_requests:write.",
                ],
            )
        self.ctx.pr_number = r.json()["number"]
        return f"PR #{self.ctx.pr_number}"

    async def stage_webhook_received(self) -> str:
        """The api got pull_request, verified the HMAC, and wrote scan state."""

        async def check():
            return await self.store.get_scan(self.ctx.scan_id)

        doc = await self._poll(
            check,
            what="the scan document",
            timeout=90,
            interval=3,
            hints=[
                "The webhook never arrived or was rejected. Check the app's",
                "  Settings -> Advanced -> Recent Deliveries for a non-2xx response.",
                "401 there means GITHUB_WEBHOOK_SECRET differs from the app's secret.",
                "No delivery at all means the Webhook URL still points at smee.",
                "Delivered 202 but no document: check the api logs for handler_failed,",
                "  and confirm the service has roles/datastore.user.",
            ],
        )
        self.ctx.check_run_id = doc.get("check_run_id")
        return f"state={doc.get('state')} check_run={self.ctx.check_run_id}"

    async def stage_check_run_created(self) -> str:
        async def check():
            r = await self._app("GET", f"/repos/{self.ctx.repo}/commits/{self.ctx.head_sha}/check-runs")
            if r.status_code != 200:
                return None
            runs = [c for c in r.json().get("check_runs", []) if c["name"] == self.settings.check_run_name]
            return runs[0] if runs else None

        run = await self._poll(
            check,
            what="the check run",
            timeout=90,
            interval=3,
            hints=[
                "The scan document exists but no check run was created.",
                "Most likely: the api is deployed WITHOUT --no-cpu-throttling, so the",
                "  background task froze after the 202. This is the classic symptom.",
                "Otherwise check the api logs for check_run_create_failed -- that means",
                "  the app lacks Checks: read & write.",
            ],
        )
        return f"{run['status']} — {(run.get('output') or {}).get('title') or '(no title)'}"

    async def stage_deployment_matched(self) -> str:
        """A preview deployment was reported and matched to this commit."""

        async def check():
            doc = await self.store.get_scan(self.ctx.scan_id)
            if not doc:
                return None
            if doc.get("state") == "no_preview":
                raise StageFailure(
                    "the scan gave up waiting for a preview deployment",
                    [
                        "No provider reported a successful deployment for this commit.",
                        "Confirm the repo is connected to Vercel/Netlify and the deploy succeeded.",
                        "Most common cause: the app lacks the Deployments: read permission,",
                        "  so deployment_status webhooks are never delivered. Check",
                        "  Settings -> Advanced -> Recent Deliveries for any deployment_status event.",
                    ],
                )
            return doc.get("preview")

        preview = await self._poll(
            check,
            what="a preview deployment",
            timeout=self.ctx.timeout,
            hints=["No preview arrived within the timeout."],
        )
        return f"{preview.get('provider')} — {preview.get('url')}"

    async def stage_scan_completed(self) -> str:
        async def check():
            doc = await self.store.get_scan(self.ctx.scan_id)
            state = (doc or {}).get("state")
            if state in ("completed", "failed", "quota_exceeded"):
                return doc
            return None

        doc = await self._poll(
            check,
            what="the scan to finish",
            timeout=self.ctx.timeout,
            hints=[
                "The scan started but never finished. Check the worker logs.",
                "Stuck in queued: Cloud Tasks cannot reach the worker. Verify tasks-invoker",
                "  has roles/run.invoker on a11y-worker, and that the api has",
                "  roles/iam.serviceAccountUser on tasks-invoker.",
                "Stuck in in_progress: look for scan_failed with retryable=true.",
            ],
        )

        state = doc["state"]
        result = doc.get("result") or {}
        if state == "quota_exceeded":
            raise StageFailure(
                f"the scan was skipped by the quota gate ({doc.get('quota_reason')})",
                [
                    "Free plan: 1 private repo, 50 private scans/month. Public repos are unmetered.",
                    "Use a public repo for smoke testing, or raise the plan in Firestore:",
                    f"  installations/{self.ctx.installation_id}.plan = 'team'",
                ],
            )
        if state == "failed":
            raise StageFailure(
                f"the scan failed: {result.get('error')}",
                [
                    "HTTP 401/403 from the preview means Vercel Deployment Protection.",
                    "  Add the bypass token: python scripts/set_bypass_secret.py <iid> <repo_id> <token>",
                    "Navigation timeouts mean the preview is slow or behind auth.",
                ],
            )
        return (
            f"{result.get('findings')} findings, {result.get('needs_review')} need review, "
            f"{result.get('duration_ms')}ms, axe {result.get('engine_version')}"
        )

    async def stage_finding_detected(self) -> str:
        """The deliberate bug was actually found, and mapped to the fixture."""
        findings = [
            d
            async for d in self.store.db.collection("scans")
            .document(self.ctx.scan_id)
            .collection("findings")
            .stream()
        ]
        docs = [f.to_dict() for f in findings]
        image_alt = [f for f in docs if f.get("rule_id") == "image-alt"]
        if not image_alt:
            raise StageFailure(
                "image-alt was not reported",
                [
                    f"The scanner reached the preview but did not see {FIXTURE_PATH}.",
                    "It scans the preview ROOT only. If your host does not serve that file",
                    "  at /a11y-smoke-test.html, the fixture was never loaded --",
                    "  this is a test limitation, not necessarily a product bug.",
                    f"Found rules: {sorted({f.get('rule_id') for f in docs}) or 'none'}",
                ],
            )
        f = image_alt[0]
        source = f.get("source") or {}
        mapped = f"{source.get('file')}:{source.get('line_start')}" if source else "unmapped"
        return (
            f"image-alt · {f.get('impact')} · WCAG "
            f"{','.join(w['id'] for w in f.get('wcag', []))} · {mapped} · "
            f"disposition={f.get('disposition')} ({f.get('disposition_reason')})"
        )

    async def stage_comment_posted(self) -> str:
        """A review comment landed on the PR, or the reason none did is sound."""
        doc = await self.store.get_scan(self.ctx.scan_id) or {}
        posting = (doc.get("result") or {}).get("posting") or {}

        r = await self._app("GET", f"/repos/{self.ctx.repo}/pulls/{self.ctx.pr_number}/comments")
        comments = r.json() if r.status_code == 200 else []
        ours = [c for c in comments if "WCAG" in (c.get("body") or "")]

        if not ours:
            if posting.get("rejected"):
                raise StageFailure(
                    f"GitHub rejected {posting['rejected']} comment anchor(s)",
                    [
                        "The line was not inside a diff hunk of the head commit.",
                        "Look for comment_rejected in the worker logs with the path and line.",
                    ],
                )
            if posting.get("skipped_head_moved"):
                raise StageFailure(
                    "posting was skipped: the PR head moved mid-scan",
                    [
                        "Something pushed to the branch while the scan ran. Re-run the smoke test.",
                    ],
                )
            findings = [
                d
                async for d in self.store.db.collection("scans")
                .document(self.ctx.scan_id)
                .collection("findings")
                .stream()
            ]
            reasons = {
                (d.to_dict().get("disposition"), d.to_dict().get("disposition_reason")) for d in findings
            }
            raise StageFailure(
                "no review comment was posted",
                [
                    "The scan found the issue but nothing reached the PR.",
                    "If every finding is disposition=drop, the source mapper could not place it:",
                    "  the fixture is plain .html, so a JSX-only mapper legitimately drops it.",
                    "  That is expected for this fixture and not a failure of the pipeline.",
                    f"Dispositions: {sorted(reasons, key=str)}",
                    "If GEMINI_API_KEY is unset, mapping is disabled by design.",
                ],
            )

        first = ours[0]
        kind = "one-click suggestion" if "```suggestion" in first["body"] else "annotation"
        return (
            f"{len(ours)} comment(s), first is a {kind} at "
            f"{first['path']}:{first.get('line')} — {first['html_url']}"
        )

    # -- driver -------------------------------------------------------------

    async def run(self) -> int:
        stages = [
            ("app installed", self.stage_app_installed),
            ("branch pushed", self.stage_branch_pushed),
            ("PR opened", self.stage_pr_opened),
            ("webhook received", self.stage_webhook_received),
            ("check run created", self.stage_check_run_created),
            ("deployment matched", self.stage_deployment_matched),
            ("scan completed", self.stage_scan_completed),
            ("finding detected", self.stage_finding_detected),
            ("comment posted", self.stage_comment_posted),
        ]
        width = max(len(n) for n, _ in stages)
        failed_at: str | None = None

        for name, fn in stages:
            print(f"  {name.ljust(width)}  … ", end="", flush=True)
            try:
                detail = await fn()
            except StageFailure as exc:
                print(f"\r  {name.ljust(width)}  FAIL")
                print(f"\n  ✗ {exc}\n")
                for hint in exc.hints:
                    print(f"      {hint}")
                failed_at = name
                break
            except Exception as exc:
                print(f"\r  {name.ljust(width)}  ERROR")
                print(f"\n  ✗ unexpected {type(exc).__name__}: {exc}\n")
                failed_at = name
                break
            print(f"\r  {name.ljust(width)}  PASS   {detail}")

        print()
        for note in self.ctx.notes:
            print(f"  note: {note}")

        if failed_at:
            print(f"  Broke at: {failed_at}")
            print(f"  Scan id : {self.ctx.scan_id or '(not reached)'}")
            if self.ctx.pr_number:
                print(f"  PR      : https://github.com/{self.ctx.repo}/pull/{self.ctx.pr_number}")
            return 1

        print("  All stages passed.")
        print(f"  PR: https://github.com/{self.ctx.repo}/pull/{self.ctx.pr_number}")
        return 0

    async def cleanup(self) -> None:
        if self.ctx.pr_number:
            await self._user(
                "PATCH", f"/repos/{self.ctx.repo}/pulls/{self.ctx.pr_number}", json={"state": "closed"}
            )
        await self._user("DELETE", f"/repos/{self.ctx.repo}/git/refs/heads/{BRANCH}")
        print(f"  cleaned up branch {BRANCH}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/repo the app is installed on")
    ap.add_argument(
        "--timeout", type=float, default=420.0, help="seconds for the preview and scan stages (default 420)"
    )
    ap.add_argument("--keep", action="store_true", help="leave the branch and PR in place")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("GH_TOKEN is required (try: export GH_TOKEN=$(gh auth token))")

    ctx = Context(repo=args.repo, timeout=args.timeout)
    smoke = Smoke(ctx, token)
    print(f"\n  Smoke test · {args.repo}\n")
    try:
        code = await smoke.run()
        if not args.keep:
            await smoke.cleanup()
        elif ctx.pr_number:
            print(f"  --keep: PR #{ctx.pr_number} and branch {BRANCH} left in place")
        return code
    finally:
        await smoke.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
