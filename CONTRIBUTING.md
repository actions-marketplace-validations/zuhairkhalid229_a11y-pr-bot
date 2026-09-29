# Contributing

Thanks for looking. This document is the short version of how the project
works and what "done" means here.

## Getting it running

You need Python 3.12+ and Node 20+.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt -r worker/requirements.txt
playwright install chromium       # the scanner tests drive a real browser

pytest -q                         # 216 tests, ~60s
pytest -q -m "not browser"        # ~5s, skips anything needing Chromium

cd web && npm install && npm run build
```

Short on disk space? Point the big caches somewhere else before installing:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright"
export PIP_CACHE_DIR="$PWD/.cache/pip"
```

No cloud credentials are needed to run the test suite. Firestore, Cloud Tasks
and GitHub are faked; Gemini is exercised only by `scripts/eval_mapper.py`,
which is opt-in and needs a key.

## How the codebase is laid out

```
app/      the public webhook service + dashboard API   (Cloud Run)
worker/   the scanner, mapper and poster               (Cloud Run, private)
web/      the dashboard                                (Next.js, Vercel)
```

A pull request travels through these in order, and each stage has one file
worth reading first:

| Stage | Start here |
|---|---|
| Webhook arrives, is verified and deduped | `app/webhooks/router.py` |
| PR and preview-deployment halves merge | `app/store/scan_state.py` |
| Page is scanned with axe-core | `worker/scanner.py` |
| Finding is traced back to JSX | `worker/mapper/mapper.py` |
| Comment is posted, deduped across pushes | `worker/poster/poster.py` |

The data contract everything writes into is `worker/schema.py`. Read that
before changing anything that produces or consumes a finding.

## The rules that actually matter

Most of this project's design exists to protect one property: **a wrong
one-click fix is worse than no fix.** A developer who commits a suggestion that
breaks their build will uninstall and never come back. Concretely:

1. **Never trust the model's output.** `worker/mapper/verify.py` re-finds the
   patch text in the real file, and `worker/mapper/decide.py` gates on checks
   we perform ourselves. A model confidence score is an input to that decision,
   never the decision.
2. **Degrade, don't guess.** When confidence is low the finding becomes a
   comment a human reads, or is dropped with a recorded reason. Silence is an
   acceptable outcome; a confident wrong answer is not.
3. **Be honest about coverage.** Automated checks catch roughly a third of WCAG
   2.2 success criteria. Anything that implies otherwise — in the UI, the
   README, or a PR comment — is a bug.
4. **No overlays, ever.** We do not ship a widget that patches a page at
   runtime. Fixes land in the user's source, reviewed by them.

## Changing the source mapper

The mapper is the risky part, so it has its own evaluation harness rather than
only unit tests. If you touch `worker/mapper/`, prompts included:

```bash
pytest tests/worker/test_mapper*.py         # offline: stage 1, verify, decide
GEMINI_API_KEY=... python scripts/eval_mapper.py
```

The eval reports four numbers against the 12 cases in
`tests/worker/mapper_cases/`:

| Metric | Target |
|---|---|
| False-suggestion rate | **< 2%** |
| Location precision | ≥ 95% |
| Recall | ≥ 70% |
| Calibration | accuracy per confidence bucket should track the bucket |

It exits non-zero if any suggestion was wrong. **A change that raises recall
and raises the false-suggestion rate is a regression, not an improvement.**
Put the before/after numbers in your PR description.

Found a case where the mapper misfires? A new directory under
`tests/worker/mapper_cases/` is a welcome contribution on its own, with or
without a fix.

## Tests

- New behaviour needs a test. New *branch* in the mapper or poster needs one
  too — those are the paths that reach real pull requests.
- Prefer testing pure functions directly (`scan_state.py`, `quota.py`,
  `plan.py`, `verify.py` are all deliberately side-effect free).
- HTTP is mocked with `respx` against the real client code, not by faking our
  own wrappers. See `tests/worker/test_reviews_api.py`.
- Mark anything that launches Chromium with `@pytest.mark.browser`.

## Commits and pull requests

- Conventional-ish subjects (`fix:`, `feat:`, `docs:`, `test:`, `chore:`) —
  helpful, not enforced.
- One logical change per PR. A drive-by refactor inside a bug fix makes the fix
  hard to review and harder to revert.
- Say what you verified, and how. "Tests pass" is less useful than "added the
  `wrapper-component` eval case; false-suggestion rate stayed 0/4".
- CI runs the Python suite, the dashboard typecheck and build, and ruff. It
  must be green.

## Reporting bugs

The most valuable accessibility bug report includes the **rendered HTML** of
the element and the **JSX that produced it**. With those two, a failing case can
be added to the eval set in minutes. A screenshot of a wrong comment is good
too — especially a wrong suggestion, which is the failure mode we care most
about.

Security issues: see [SECURITY.md](SECURITY.md). Please do not open a public
issue for those.

## Licence

Contributions are accepted under the Apache License 2.0 (see `LICENSE`). There
is no CLA. Note that `worker/vendor/axe.min.js` is third-party MPL-2.0 code and
must stay unmodified — see `NOTICE` before touching it.
