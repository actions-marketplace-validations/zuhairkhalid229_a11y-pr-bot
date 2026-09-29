# a11y-pr-bot

**WCAG 2.2 and EN 301 549 checks on every pull request, with one-click fixes for your JSX.**

[![CI](https://github.com/zuhairkhalid229/a11y-pr-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/zuhairkhalid229/a11y-pr-bot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![axe-core](https://img.shields.io/badge/axe--core-4.13.0-663399.svg)](https://github.com/dequelabs/axe-core)

A GitHub App that scans your preview deployment with [axe-core](https://github.com/dequelabs/axe-core),
traces each violation back to the JSX that produced it, and posts a review
comment you can commit with one click.

```
open a PR → Vercel/Netlify deploys a preview → we scan it → review comments on the diff
```

Most accessibility tools hand you a rule id and a CSS selector from the rendered
DOM, and leave you to find the component. This one reads the JSX in your diff
and gives you the file, the line, and the patch.

---

## What it looks like

A single review per push, not one comment per finding:

> **critical · Images must have alternative text** — WCAG 1.1.1 · EN 301 549 9.1.1.1
>
> Logo links to the homepage; the alt text names the destination.
>
> ````suggestion
> <img src="/logo.svg" alt="Acme home" className="h-8 w-auto" />
> ````

On the next push, fixed issues get a ✅ reply and their thread resolved. Issues
still open are listed, not re-posted. Nothing new and nothing fixed means no
comment at all.

## What it does not do

The accessibility tooling market has earned some scepticism, so:

- **Automated checks catch roughly a third of WCAG 2.2 success criteria.**
  Nothing that runs without a human can tell you whether your alt text is
  *meaningful* or whether your focus order makes sense. Findings the scanner
  cannot decide are reported separately as "needs manual review" rather than
  quietly dropped.
- **This is not an overlay.** No widget, no script tag, no runtime patching.
  Every fix is a change in your own source, reviewed by you.
- **Passing is not a legal compliance certificate.** It is evidence of
  conformance testing against a documented standard.

## Using it

See **[INSTALL.md](INSTALL.md)** — permissions, setup, pricing, troubleshooting.
Running your own instance: **[DEPLOY.md](DEPLOY.md)**.

---

## How it works

Two Cloud Run services and a dashboard.

```
GitHub ──webhook──► app/  (public)  ──Cloud Tasks──► worker/  (private)
                      │                                 │
                      └────────── Firestore ────────────┘
                                     ▲
                      web/  (Vercel) ┘   reads with the viewer's own credentials
```

A pull request travels through five stages, each with one file worth reading first:

| Stage | Start here |
|---|---|
| Webhook verified (HMAC) and deduped; 202 in under a second | [`app/webhooks/router.py`](app/webhooks/router.py) |
| PR and preview-deployment halves merge, whichever lands second | [`app/store/scan_state.py`](app/store/scan_state.py) |
| Preview loaded in Chromium, axe-core injected | [`worker/scanner.py`](worker/scanner.py) |
| Finding traced back to JSX, patch drafted | [`worker/mapper/mapper.py`](worker/mapper/mapper.py) |
| Comment posted, deduped across pushes | [`worker/poster/poster.py`](worker/poster/poster.py) |

Everything writes into one contract: [`worker/schema.py`](worker/schema.py).

### The design constraint everything serves

**A wrong one-click fix is worse than no fix.** A developer who commits a
suggestion that breaks their build uninstalls and never returns. So:

- The model returns a **candidate index, not a file path** — it cannot name a
  file the deterministic search did not find.
- Its patch text is **re-verified against the real file** before anything is
  posted, and must sit inside a diff hunk and pass a structural balance check.
- Below threshold, a finding degrades to a comment a human reads, or is dropped
  **with the reason recorded**. Silence is an acceptable outcome.
- A source line that renders several elements (a `.map()`) is never a one-click
  suggestion — a literal fix would be wrong for the siblings.

The mapper is measured, not assumed. `scripts/eval_mapper.py` runs 12 curated
cases and reports four numbers; the gate is **zero wrong suggestions**.

Latest run (`gemini-3.1-flash-lite`, 2026-09-29):

| Metric | Result | Target |
|---|---|---|
| False-suggestion rate | **0 / 4** | < 2% |
| Location precision | 10 / 10 | ≥ 95% |
| Recall | 10 / 11 | ≥ 70% |

## Development

```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements-dev.txt -r worker/requirements.txt
playwright install chromium

pytest -q                     # 216 tests, ~60s
pytest -q -m "not browser"    # ~5s, no Chromium needed
ruff check . && ruff format --check .

cd web && npm install && npm run build
```

No cloud credentials are needed. Firestore, Cloud Tasks and GitHub are faked;
Gemini is exercised only by the opt-in eval harness.

Full guide, including the rules for changing the mapper:
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

### Local end-to-end loop

```bash
gcloud emulators firestore start --host-port=localhost:8081   # terminal 1
npx smee-client -u https://smee.io/<channel> \
  -t http://localhost:8080/webhooks/github                    # terminal 2
FIRESTORE_EMULATOR_HOST=localhost:8081 \
  uvicorn app.main:app --reload --port 8080                   # terminal 3
```

The worker also runs standalone, with no GitHub or Firestore involved:

```bash
uvicorn worker.main:app --port 8081
curl -s localhost:8081/tasks/scan -H 'content-type: application/json' \
  -d '{"scan_id":"local","url":"https://example.com"}' | python -m json.tool
```

### Smoke test against a real installation

```bash
export GH_TOKEN=$(gh auth token)
python scripts/smoke_test.py --repo owner/repo
```

Opens a PR with an `<img>` missing `alt` and walks all nine stages, stopping at
the first failure with the specific thing to check.

### Upgrading axe-core

```bash
bash scripts/vendor_axe.sh 4.14.0
pytest tests/worker     # rule ids and tags move between releases
```

The vendored copy is verified against a recorded sha256 at import, so a partial
or tampered download fails at startup rather than mid-scan.

## Quota

`app/store/quota.py` is the whole policy. **Public repositories are never
metered** — they are the distribution channel. The meter exists for private
repos: free is 1 repo and 50 scans a month.

## Project layout

```
app/       webhook service + dashboard API
  webhooks/    HMAC verification, PR and deployment handlers
  store/       Firestore access, scan state machine, quota
  github/      App JWT → installation token, Checks API
  api/         dashboard endpoints (link, bypass token)
worker/    scanner, mapper, poster
  scanner.py   Playwright + axe-core
  mapper/      finding → JSX source → patch
  poster/      batched PR review, dedupe across pushes
  vendor/      axe-core 4.13.0 (MPL-2.0, unmodified)
web/       Next.js dashboard
scripts/   vendoring, mapper evaluation, smoke test
```

## Licence

Apache 2.0 — see [LICENSE](LICENSE).

`worker/vendor/axe.min.js` is third-party code from
[Deque Systems](https://github.com/dequelabs/axe-core) under the **Mozilla
Public License 2.0**, redistributed unmodified. See [NOTICE](NOTICE) for what
that means in practice before you touch that file.
